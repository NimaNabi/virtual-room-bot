"""/ask — natural-language questions answered from real bot data via read-only tools.

Flow: question → model picks tools → tools read DB/Discord state → model explains.
Mutations: the model can only call propose_repair (a dry run). Applying is a button that runs the
same authorization + re-plan + apply + verify path as /baseline repair. Raw model text never executes.
"""
from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..ai.client import AIUnavailable, ChatClient
from ..ai.tools import NEUTRAL_ACCESS, Tools, dumps, schemas_for
from ..authz import Level
from ..perms.audit import plan_repair
from ..repair import apply_changes
from ..ui import COLORS, ConfirmView, chunk_lines

log = logging.getLogger("vrbot.ai")

SYSTEM = """You are the management assistant for the Discord server "{server}".
Answer ONLY from tool results. If the tools do not show something, say you don't have that data — never guess.
Rules:
- Use tools for every factual claim (members, permissions, logs, changes). Resolve names with find_member / list_channels first when unsure.
- For "who did X": report actor_confidence honestly. 'confirmed' = Discord audit log names the moderator;
  'likely' = correlation (Discord does not record the target of voice moves/disconnects); 'self'/'unknown' = no moderator action found.
- For permission questions use explain_permission and quote the reason and recommended fix.
- You cannot change anything yourself. For fix requests call propose_repair and tell the user to review the dry run and press Apply.
- CURRENT state comes only from permission_problems / explain_permission / find_member / server_status. Logs and
  Guardian alerts are HISTORY: never claim something is fixed or still broken from them without checking current state.
- search_logs returns newest first. Before describing a sequence ("changed from A to B"), sort by ts and read each
  event's before/after (or added/removed) exactly; do not infer direction.
- Be concise; use Discord markdown; times are UTC ISO — present them readably. Trust levels: {trust_levels}.
"""
FALLBACK = ("AI is unavailable right now ({err}). Core commands still work: `/permissions why`, `/logs user`, "
            "`/logs action`, `/server doctor`, `/baseline check`.")


class AI(commands.GroupCog, group_name="ai", group_description="Ask the bot about the server"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()
        s = bot.settings
        self.client = None
        if s.ai_enabled and bot.cfg.ai.enabled and s.ai_base_url:
            self.client = ChatClient(s.ai_base_url, s.ai_api_key, [s.ai_model] + s.ai_fallback_models)

    async def cog_unload(self):
        if self.client:
            await self.client.close()

    def status_text(self) -> str:
        if not self.client:
            return "disabled (set AI_ENABLED=true and AI_BASE_URL in .env)"
        if self.client.last_error:
            return f"last call failed ({self.client.last_error[:80]})"
        return f"configured → {self.client.models[0]}" + (f" (last used {self.client.last_model})" if self.client.last_model else "")

    async def answer(self, question: str, asker: discord.Member) -> tuple[str, Tools]:
        tools = Tools(self.bot, asker.id)
        if not self.client:
            return FALLBACK.format(err="not configured"), tools
        trust_levels = (", ".join(f"{k}=@{c.role} (rank {c.rank})" for k, c in self.bot.cfg.baseline.trust_levels.items()) or "none configured") \
            if tools.owner_view else "(internal — never discuss)"
        system = SYSTEM.format(server=self.bot.guild.name, trust_levels=trust_levels)
        if not tools.owner_view:
            system += ("\nThe person asking is a regular member. Never mention access tiers, trust levels, rankings or who is "
                       f"'closer'. If asked why someone else has access and they don't, answer: \"{NEUTRAL_ACCESS}\"")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"[asked by {asker.display_name}]\n{question}"}]
        try:
            for _ in range(self.bot.cfg.ai.max_tool_rounds):
                msg = await self.client.complete(messages, schemas_for(tools.owner_view))
                calls = msg.get("tool_calls") or []
                if not calls:
                    return (msg.get("content") or "").strip() or "(no answer)", tools
                messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for tc in calls:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    result = await tools.call(fn.get("name", ""), args if isinstance(args, dict) else {})
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": dumps(result)})
            messages.append({"role": "user", "content": "Answer now with the information gathered; no more tools."})
            msg = await self.client.complete(messages)
            return (msg.get("content") or "").strip() or "(no answer)", tools
        except AIUnavailable as e:
            return FALLBACK.format(err=str(e)[:150]), tools

    @app_commands.command(name="ask", description="Ask the bot about the server (uses real logs and permission data)")
    @app_commands.describe(question="e.g. Why can't Sam send messages in #general?", public="Post the answer visibly")
    async def ask(self, interaction: discord.Interaction, question: str, public: bool = False):
        need = Level.parse(self.bot.cfg.access.ai_min_level)
        if self.bot.level_of(interaction.user) < need:
            raise app_commands.CheckFailure(f"/ask needs **{need.name.title()}** access.")
        await interaction.response.defer(ephemeral=not public, thinking=True)
        text, tools = await self.answer(question, interaction.user)
        await self.bot.db.add_event(type="ai_query", category="bot", guild_id=self.bot.guild.id,
                                    actor_id=interaction.user.id, actor_name=interaction.user.display_name,
                                    actor_confidence="confirmed", details={"tools": tools.used,
                                                                           "model": self.client.last_model if self.client else None},
                                    source="bot")
        pages = chunk_lines(text.split("\n"), 3900)
        embeds = [discord.Embed(description=p, color=COLORS["BRAND"]) for p in pages[:4]]
        embeds[0].title = f"❓ {question[:240]}"
        if tools.used:
            embeds[-1].set_footer(text="Data: " + ", ".join(dict.fromkeys(tools.used)) +
                                  (f" • model {self.client.last_model}" if self.client and self.client.last_model else ""))
        view = None
        if tools.proposal and tools.proposal[0].changes and not tools.proposal[0].blocked:
            plan, trust_level = tools.proposal
            view = self._apply_view(interaction, plan, trust_level)
        kwargs = {"embeds": embeds, "ephemeral": not public, "allowed_mentions": discord.AllowedMentions.none()}
        if view:
            kwargs["view"] = view
        await interaction.followup.send(**kwargs)

    def _apply_view(self, interaction, plan, trust_level):
        need = Level.parse(self.bot.cfg.access.repair_min_level)

        async def do(i: discord.Interaction):
            fresh = plan_repair(self.bot.model(), self.bot.cfg, self.bot.user.id, scope=trust_level)
            if [c.to_dict() for c in fresh.changes] != [c.to_dict() for c in plan.changes] or not fresh.safe:
                await i.followup.send("State changed or plan is no longer safe. Run `/baseline repair`.", ephemeral=True)
                return
            res = await apply_changes(self.bot, fresh.changes, source="repair", actor=i.user,
                                      summary=f"AI-proposed trust level repair ({trust_level or 'all'})")
            from .trust_level import _result_embed
            await i.followup.send(embed=_result_embed(res), ephemeral=True)

        def allowed(i: discord.Interaction) -> bool:
            return self.bot.level_of(i.user) >= need

        return ConfirmView(interaction.user.id, do, confirm_label=f"Apply {len(plan.changes)} change(s)", allowed=allowed)

    @app_commands.command(name="status", description="Check the AI gateway connection")
    async def ai_status(self, interaction: discord.Interaction):
        ok = await self.client.health() if self.client else False
        await interaction.response.send_message(f"AI: {self.status_text()} — gateway {'reachable ✅' if ok else 'unreachable ❌'}",
                                                ephemeral=True)


async def setup(bot):
    await bot.add_cog(AI(bot))
