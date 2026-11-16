import asyncio
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

import discord
from discord import app_commands

log = logging.getLogger("alune")
mentions = discord.AllowedMentions.none()
LINE_CODES = {"TOP": "TOP", "JG": "JG", "JGL": "JG", "JUNGLE": "JG", "MID": "MID",
              "AD": "AD", "ADC": "AD", "SUP": "SUP", "ALL": "ALL"}


class UserError(Exception):
    pass


def normalize(account, peak, positions):
    account = account.strip()
    peak = peak.strip().upper()
    lines = [LINE_CODES.get(line) for line in re.split(r"[\s,/]+", positions.strip().upper())]
    if not re.fullmatch(r"[^#/\r\n]+#[\w]{2,5}", account):
        raise UserError("본인 명의 라이엇 ID를 닉#태그 형식으로 입력해 주세요.")
    if not re.fullmatch(r"(?:[IBSGPED][1-4]|(?:GM|M|C)\d{1,4})", peak):
        raise UserError("탑레는 P1, E2, M215, GM800, C1200처럼 입력해 주세요.")
    if not all(lines) or len(lines) > 5 or len(set(lines)) != len(lines):
        raise UserError("라인을 중복 없이 입력해 주세요. 예) TOP, MID")
    if "ALL" in lines and len(lines) != 1:
        raise UserError("ALL은 단독으로 입력해 주세요.")
    positions = ",".join(lines)
    nickname = f"{account}/{peak}/{positions}"
    if len(nickname.encode("utf-16-le")) // 2 > 32:
        raise UserError("완성된 닉네임은 최대 32자입니다. 라인 약어와 입력 내용을 확인해 주세요.")
    return account, peak, positions, nickname


class Database:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY, member INTEGER NOT NULL, account TEXT NOT NULL,
                peak TEXT NOT NULL, positions TEXT NOT NULL, nickname TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'pending', reviewer INTEGER, marker INTEGER,
                review_message INTEGER
            );
            CREATE TABLE IF NOT EXISTS presence (
                member INTEGER PRIMARY KEY, joins INTEGER NOT NULL DEFAULT 0,
                leaves INTEGER NOT NULL DEFAULT 0, kicks INTEGER NOT NULL DEFAULT 0,
                suppress_remove INTEGER NOT NULL DEFAULT 0, joined_at TEXT
            );
            CREATE TABLE IF NOT EXISTS panels (name TEXT PRIMARY KEY, message INTEGER NOT NULL);
        """)

    def run(self, sql, *args):
        return self.db.execute(sql, args)

    def latest(self, member):
        return self.run(
            "SELECT * FROM applications WHERE member=? ORDER BY id DESC LIMIT 1", member
        ).fetchone()

    def panel(self, name):
        row = self.run("SELECT message FROM panels WHERE name=?", name).fetchone()
        return row["message"] if row else None

    def record_panel(self, name, message):
        self.run("INSERT OR REPLACE INTO panels VALUES (?, ?)", name, message)


class Registration(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="📝 닉네임 신청", style=discord.ButtonStyle.primary,
                       custom_id="alune:apply")
    async def apply(self, interaction, button):
        if not self.bot.valid_panel(interaction, "registration", self.bot.registration_id):
            return await interaction.response.send_message("최신 등록 안내를 이용해 주세요.", ephemeral=True)
        await interaction.response.send_modal(Application(self.bot))

    @discord.ui.button(label="내 신청 상태", style=discord.ButtonStyle.secondary,
                       custom_id="alune:status")
    async def status(self, interaction, button):
        await self.bot.show_status(interaction)


class Application(discord.ui.Modal, title="내전 참가 신청 (탑레 기준)"):
    account = discord.ui.TextInput(label="본인 명의 계정 닉#태그", placeholder="소환사명#KR1", max_length=30)
    peak = discord.ui.TextInput(label="최근 3년간 최고 티어 (마감 기준 X)", placeholder="P1, E2, M215, GM800, C1200", max_length=8)
    positions = discord.ui.TextInput(label="주라인 / 부라인 (또는 ALL)", placeholder="TOP, MID 또는 ALL", max_length=30)

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction):
        await self.bot.reply(interaction, self.bot.submit, self.account.value,
                             self.peak.value, self.positions.value)


class Review(discord.ui.View):
    def __init__(self, bot, disabled=False):
        super().__init__(timeout=None)
        self.bot = bot
        for item in self.children:
            item.disabled = disabled

    @discord.ui.button(label="✅ 수락", style=discord.ButtonStyle.success, custom_id="alune:accept")
    async def accept(self, interaction, button):
        await self.bot.reply(interaction, self.bot.review, True)

    @discord.ui.button(label="✖ 거절", style=discord.ButtonStyle.danger, custom_id="alune:reject")
    async def reject(self, interaction, button):
        await self.bot.reply(interaction, self.bot.review, False)


class Rules(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="✅ 규칙을 모두 읽었어요", style=discord.ButtonStyle.success,
                       custom_id="alune:rules")
    async def confirm(self, interaction, button):
        await self.bot.reply(interaction, self.bot.confirm_rules)


class Alune(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents, allowed_mentions=mentions)
        self.guild_id = int(os.environ["GUILD_ID"])
        self.registration_id = int(os.environ["REGISTRATION_CHANNEL_ID"])
        self.rules_id = int(os.environ["RULES_CHANNEL_ID"])
        self.log_id = int(os.environ["MEMBER_LOG_CHANNEL_ID"])
        self.unverified_id = int(os.environ["UNVERIFIED_ROLE_ID"])
        self.confirmed_id = int(os.environ["RULES_CONFIRMED_ROLE_ID"])
        self.scrim_id = int(os.environ["SCRIM_ROLE_ID"])
        self.reviewer_roles = {
            int(member): int(role)
            for member, role in json.loads(os.environ["APPROVER_ROLE_MAP"]).items()
        }
        if len({self.unverified_id, self.confirmed_id, self.scrim_id}) != 3:
            raise ValueError("미인증, 규칙확인완료, 내전 역할 ID는 서로 달라야 합니다.")
        self.data = Database(os.getenv("DB_PATH", "data/alune.sqlite3"))
        self.lock = asyncio.Lock()
        self.tree = app_commands.CommandTree(self)
        self.guild_ref = discord.Object(id=self.guild_id)
        self.tree.add_command(registration_command, guild=self.guild_ref)
        self.tree.add_command(rules_command, guild=self.guild_ref)

    async def setup_hook(self):
        self.add_view(Registration(self))
        self.add_view(Review(self))
        self.add_view(Rules(self))
        await self.tree.sync(guild=self.guild_ref)

    async def on_ready(self):
        log.info("Connected as %s", self.user)

    async def reply(self, interaction, operation, *args):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if interaction.guild_id != self.guild_id:
                raise UserError("여 운 서버에서만 사용할 수 있어요.")
            result = await operation(interaction, *args)
        except UserError as exc:
            result = str(exc)
        except (discord.HTTPException, sqlite3.Error):
            log.exception("Interaction failed: %s", operation.__name__)
            result = "처리 중 오류가 발생했어요. 채널·역할 권한을 확인한 뒤 다시 시도해 주세요."
        await interaction.followup.send(result, ephemeral=True)

    def valid_panel(self, interaction, name, channel_id):
        return (
            interaction.guild_id == self.guild_id
            and interaction.channel_id == channel_id
            and interaction.message is not None
            and interaction.message.id == self.data.panel(name)
        )

    def channel(self, guild, channel_id, private=False):
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise UserError(f"텍스트 채널 설정을 확인해 주세요. 채널 ID {channel_id}")
        if private:
            if channel.permissions_for(guild.default_role).view_channel:
                raise UserError(f"<#{channel_id}> 채널의 @everyone 보기 권한을 차단해 주세요.")
            # Explicit overwrites can let ordinary roles or members bypass @everyone.
            for target, overwrite in channel.overwrites.items():
                if not overwrite.view_channel or target == guild.me:
                    continue
                permissions = getattr(target, "permissions", None) if isinstance(target, discord.Role) else getattr(target, "guild_permissions", None)
                if permissions is None or not permissions.administrator:
                    raise UserError(f"<#{channel_id}>의 보기 허용은 관리자 역할과 알룬에만 설정해 주세요.")
        return channel

    def role(self, guild, role_id, marker=False):
        role = guild.get_role(role_id)
        if role is None or not role.is_assignable():
            raise UserError(f"알룬이 관리할 수 있는 역할 ID인지 확인해 주세요. 역할 ID {role_id}")
        if marker and (role.permissions.value or role_id in {self.scrim_id, self.confirmed_id, self.unverified_id}
                       or any(channel.overwrites_for(role).pair()[0].value for channel in guild.channels)):
            raise UserError("담당 관리자 이름 역할은 권한이 없는 표시용 역할이어야 해요.")
        return role

    def rules_notice(self):
        return (f"🌙 신청이 승인됐어요. <#{self.rules_id}>에서 서버·내전 규칙을 모두 읽고 "
                "확인 버튼을 눌러 주세요. 규칙 확인을 마치면 내전 역할을 받고 참가할 수 있어요.")

    async def show_status(self, interaction):
        if not self.valid_panel(interaction, "registration", self.registration_id):
            return await interaction.response.send_message("최신 등록 안내를 이용해 주세요.", ephemeral=True)
        row = self.data.latest(interaction.user.id)
        replies = {
            "pending": "관리자가 신청을 검토 중이에요.",
            "approving": "관리자가 신청을 처리 중이에요.",
            "approved": self.rules_notice(),
            "confirmed": "규칙 확인이 완료됐어요. 내전에 참가할 수 있어요.",
            "rejected": "신청이 거절됐어요. 내용을 확인한 후 다시 신청할 수 있어요.",
        }
        await interaction.response.send_message(
            replies.get(row["stage"] if row else None, "닉네임 신청 버튼으로 먼저 신청해 주세요."),
            ephemeral=True,
        )

    async def post_panel(self, guild, name):
        if name == "registration":
            channel = self.channel(guild, self.registration_id)
            embed = discord.Embed(title="🎮 내전 참가 신청 및 닉네임 등록", color=0x8D7CE4)
            embed.add_field(name="📌 작성 양식",
                            value="`본인 명의 계정 닉#태그 / 최근 3년 탑레 / 주·부라인`", inline=False)
            embed.add_field(name="⚠️ 주의사항", value=(
                "• 시즌 마감 티어가 아닌 최근 3년간 달성한 최고 티어를 적어 주세요.\n"
                "• 위장 티어 적발 시 즉시 추방 처리됩니다. (FOW S14-1 이후 탑레 대조)\n"
                f"• 관리자 승인 후 <#{self.rules_id}>에서 규칙 확인을 완료하면 내전 역할이 지급돼요."),
                inline=False)
            embed.set_footer(text="신청 후 ‘내 신청 상태’에서 승인 여부와 다음 단계를 확인할 수 있어요.")
            view = Registration(self)
        else:
            channel = self.channel(guild, self.rules_id)
            embed = discord.Embed(title="🌙 서버·내전 규칙 확인",
                                  description="이 채널의 서버·내전 규칙을 모두 읽은 뒤 아래 버튼을 눌러 주세요.",
                                  color=0x8D7CE4)
            view = Rules(self)
        previous = self.data.panel(name)
        message = await channel.send(embed=embed, view=view)
        self.data.record_panel(name, message.id)
        if previous:
            try:
                await channel.get_partial_message(previous).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Old %s panel could not be deleted; its buttons are inactive", name)

    def review_embed(self, row):
        state = row["stage"]
        titles = {"approved": "✅ 승인 완료", "rejected": "❌ 거절 완료"}
        embed = discord.Embed(title="📥 내전 참가 승인 요청 (탑레 확인 필요)",
                              description="⚠️ 위장 티어 여부 검증 (FOW S14-1~현재 기준)\n"
                              f"[🔗 FOW 전적 바로가기](https://www.fow.lol/find/kr/{quote(row['account'].replace('#', '-'), safe='')})",
                              color={"approved": 0x2ECC71, "rejected": 0xE74C3C}.get(state, 0x8D7CE4))
        embed.add_field(name="디스코드 유저", value=f"<@{row['member']}> (`{row['member']}`)", inline=False)
        clean = discord.utils.escape_markdown
        embed.add_field(name="적용될 닉네임", value=clean(row["nickname"]), inline=False)
        embed.add_field(name="제출 내용", value=(
            f"• 계정 {clean(row['account'])}\n• 3개년 탑레 {row['peak']}\n• 주/부라인 {row['positions']}"),
            inline=False)
        if state in titles:
            embed.add_field(name="승인 결과", value=f"{titles[state]} · 처리자 <@{row['reviewer']}>", inline=False)
        return embed

    async def submit(self, interaction, account, peak, positions):
        if interaction.channel_id != self.registration_id:
            raise UserError("자기소개 채널의 닉네임 등록 안내에서 신청해 주세요.")
        values = normalize(account, peak, positions)
        async with self.lock:
            member = interaction.user
            row = self.data.latest(member.id)
            if row and row["stage"] in {"pending", "approving", "approved", "confirmed"}:
                raise UserError("이미 신청했어요. 등록 안내의 ‘내 신청 상태’에서 확인해 주세요.")
            if self.unverified_id not in {role.id for role in member.roles}:
                raise UserError("미인증 역할이 없어요. 관리자에게 문의해 주세요.")
            channel = self.channel(interaction.guild, self.registration_id)
            request = self.data.run(
                "INSERT INTO applications (member, account, peak, positions, nickname) VALUES (?, ?, ?, ?, ?)",
                member.id, *values).lastrowid
            try:
                message = await channel.send(embed=self.review_embed(self.data.latest(member.id)), view=Review(self))
            except discord.HTTPException:
                self.data.run("DELETE FROM applications WHERE id=?", request)
                raise
            self.data.run("UPDATE applications SET review_message=? WHERE id=?", message.id, request)
        return "신청을 접수했어요. 등록 안내의 ‘내 신청 상태’를 누르면 본인만 승인 결과와 다음 단계 안내를 볼 수 있어요."

    async def review(self, interaction, accept):
        if not interaction.user.guild_permissions.administrator or interaction.message is None:
            raise UserError("관리자만 신청을 수락하거나 거절할 수 있어요.")
        guild = interaction.guild
        async with self.lock:
            row = self.data.run("SELECT * FROM applications WHERE review_message=?",
                                interaction.message.id).fetchone()
            if not row or row["stage"] == "closed":
                raise UserError("이미 만료된 신청이에요.")
            if row["stage"] in {"approved", "confirmed", "rejected"}:
                await interaction.message.edit(view=Review(self, disabled=True))
                return "이미 처리된 신청이에요."
            if row["stage"] == "approving" and (not accept or row["reviewer"] != interaction.user.id):
                raise UserError("승인을 시작한 관리자가 수락 버튼을 다시 눌러 마저 처리해야 해요.")
            if accept:
                marker_id = row["marker"] or self.reviewer_roles.get(interaction.user.id)
                if marker_id is None:
                    raise UserError("이 관리자의 이름 역할 ID를 APPROVER_ROLE_MAP에 먼저 설정해 주세요.")
                marker = self.role(guild, marker_id, marker=True)
                try:
                    member = await guild.fetch_member(row["member"])
                except discord.NotFound:
                    raise UserError("신청자가 서버에 없어요.") from None
                self.data.run("UPDATE applications SET stage='approving', reviewer=?, marker=? WHERE id=?",
                              interaction.user.id, marker.id, row["id"])
                await member.edit(nick=row["nickname"], reason="알룬 내전 참가 신청 승인")
                await member.add_roles(marker, reason="알룬 내전 신청 승인 담당자")
            state = "approved" if accept else "rejected"
            self.data.run("UPDATE applications SET stage=?, reviewer=? WHERE id=?",
                          state, interaction.user.id, row["id"])
            current = self.data.latest(row["member"])
            failures = []
            try:
                await interaction.message.edit(embed=self.review_embed(current), view=Review(self, disabled=True))
            except discord.HTTPException:
                log.exception("Could not refresh review message")
                failures.append("검토 임베드 갱신 실패")
            if not accept:
                return "신청을 거절했어요." + (f" ({', '.join(failures)})" if failures else "")
            try:
                await self.post_panel(guild, "registration")
            except (discord.HTTPException, UserError):
                log.exception("Registration panel repost failed")
                failures.append("등록 임베드 재게시 실패. /닉네임등록 으로 다시 띄울 수 있어요.")
            return "신청을 승인했어요." + (" " + " ".join(failures) if failures else "")

    async def confirm_rules(self, interaction):
        if not self.valid_panel(interaction, "rules", self.rules_id):
            raise UserError("규칙 채널의 최신 확인 버튼을 이용해 주세요.")
        async with self.lock:
            row = self.data.latest(interaction.user.id)
            if row and row["stage"] == "confirmed":
                return "이미 규칙 확인을 완료했어요."
            if not row or row["stage"] != "approved":
                raise UserError("닉네임 신청이 관리자에게 승인된 뒤에 규칙 확인을 완료할 수 있어요.")
            guild = interaction.guild
            confirmed = self.role(guild, self.confirmed_id)
            scrim = self.role(guild, self.scrim_id)
            unverified = self.role(guild, self.unverified_id)
            member = await guild.fetch_member(interaction.user.id)
            # Role endpoints preserve roles managed by other bots and can be retried.
            await member.add_roles(confirmed, reason="알룬 서버·내전 규칙 확인")
            await member.add_roles(scrim, reason="알룬 내전 참가 자격 부여")
            await member.remove_roles(unverified, reason="알룬 인증 완료")
            self.data.run("UPDATE applications SET stage='confirmed' WHERE id=?", row["id"])
        return "규칙 확인을 완료했어요. 이제 내전에 참가할 수 있어요!"

    async def log_event(self, guild, user, title, color, detail=None):
        try:
            channel = self.channel(guild, self.log_id, private=True)
            row = self.data.run("SELECT * FROM presence WHERE member=?", user.id).fetchone()
            embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
            embed.description = f"<@{user.id}> · `{user.id}`"
            embed.add_field(name="누적 입퇴장", value=(
                f"입장 **{row['joins']}회** · 퇴장 **{row['leaves']}회** · "
                f"알룬 자동 추방 **{row['kicks']}회**"), inline=False)
            if detail:
                embed.add_field(name="처리 내용", value=detail, inline=False)
            await channel.send(embed=embed)
        except (discord.HTTPException, UserError):
            log.exception("Could not post member event %s for %s", title, user.id)

    async def on_member_join(self, member):
        if member.guild.id != self.guild_id or member.bot:
            return
        async with self.lock:
            guild = member.guild
            stamp = member.joined_at.isoformat() if member.joined_at else None
            row = self.data.run("SELECT * FROM presence WHERE member=?", member.id).fetchone()
            if row and stamp and row["joined_at"] == stamp:
                return
            self.data.run("""
                INSERT INTO presence(member, joins, joined_at) VALUES (?, 1, ?)
                ON CONFLICT(member) DO UPDATE SET joins=joins+1,
                    joined_at=excluded.joined_at, suppress_remove=0
            """, member.id, stamp)
            await self.log_event(guild, member, "📥 서버 입장", 0x2ECC71)
            if row and row["leaves"] >= 3:
                self.data.run("UPDATE presence SET suppress_remove=suppress_remove+1 WHERE member=?", member.id)
                try:
                    await member.kick(reason="알룬 자동 추방 · 누적 퇴장 3회 이상")
                except discord.HTTPException:
                    self.data.run("UPDATE presence SET suppress_remove=suppress_remove-1 WHERE member=?", member.id)
                    await self.log_event(guild, member, "⚠️ 자동 추방 실패", 0xF39C12,
                                         "알룬의 멤버 추방 권한과 역할 순서를 확인해 주세요.")
                else:
                    self.data.run("UPDATE presence SET kicks=kicks+1 WHERE member=?", member.id)
                    await self.log_event(guild, member, "🚫 자동 추방", 0xE74C3C,
                                         "누적 퇴장 3회 이상인 유저가 재입장했어요.")
                    return
            try:
                await member.add_roles(self.role(guild, self.unverified_id), reason="알룬 신규 입장")
            except (UserError, discord.HTTPException):
                await self.log_event(guild, member, "⚠️ 미인증 역할 지급 실패", 0xF39C12,
                                     "미인증 역할 ID와 알룬의 역할 관리 권한을 확인해 주세요.")
                log.exception("Could not assign unverified role")

    async def on_raw_member_remove(self, payload):
        if payload.guild_id != self.guild_id or payload.user.bot:
            return
        async with self.lock:
            user = payload.user
            row = self.data.run("SELECT * FROM presence WHERE member=?", user.id).fetchone()
            self.data.run("UPDATE applications SET stage='closed' WHERE member=? AND stage != 'closed'", user.id)
            if row and row["suppress_remove"]:
                self.data.run("UPDATE presence SET suppress_remove=suppress_remove-1 WHERE member=?", user.id)
                return
            self.data.run("""
                INSERT INTO presence(member, leaves) VALUES (?, 1)
                ON CONFLICT(member) DO UPDATE SET leaves=leaves+1
            """, user.id)
            guild = self.get_guild(self.guild_id)
            if guild:
                await self.log_event(guild, user, "📤 서버 퇴장", 0xE67E22)


async def admin_panel(interaction, name):
    if not interaction.user.guild_permissions.administrator:
        raise UserError("관리자만 게시할 수 있어요.")
    bot = interaction.client
    async with bot.lock:
        await bot.post_panel(interaction.guild, name)
    return "안내 임베드를 게시했어요."


@app_commands.command(name="닉네임등록", description="자기소개 채널에 알룬 닉네임 등록 임베드를 게시해요.")
@app_commands.default_permissions(administrator=True)
async def registration_command(interaction: discord.Interaction):
    await interaction.client.reply(interaction, admin_panel, "registration")


@app_commands.command(name="규칙확인", description="서버·내전 규칙 채널에 알룬 규칙 확인 버튼을 게시해요.")
@app_commands.default_permissions(administrator=True)
async def rules_command(interaction: discord.Interaction):
    await interaction.client.reply(interaction, admin_panel, "rules")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Alune().run(os.environ["DISCORD_TOKEN"])
