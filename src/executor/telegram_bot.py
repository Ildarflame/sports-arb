"""Telegram bot for notifications and commands."""

from __future__ import annotations

import logging
from datetime import datetime, UTC

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src.executor.models import ExecutionResult, ExecutionStatus

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends notifications and handles commands via Telegram."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot: Bot | None = None
        self._app: Application | None = None

    def _get_bot(self) -> Bot:
        """Get or create bot instance."""
        if self._bot is None:
            self._bot = Bot(token=self.bot_token)
        return self._bot

    async def send(self, message: str) -> None:
        """Send a message to configured chat."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured, skipping notification")
            return

        try:
            bot = self._get_bot()
            await bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    async def notify_execution(
        self,
        result: ExecutionResult,
        event_title: str,
        roi: float,
        profit: float,
        poly_balance: float = 0,
        kalshi_balance: float = 0,
    ) -> None:
        """Send execution result notification."""
        msg = self._format_execution_message(
            result, event_title, roi, profit, poly_balance, kalshi_balance
        )
        await self.send(msg)

    async def notify_kill_switch(self, reason: str, daily_trades: int, pnl: float) -> None:
        """Send kill switch activation notification."""
        msg = (
            "🛑 <b>KILL SWITCH ACTIVATED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Reason: {reason}\n"
            f"Trades today: {daily_trades}\n"
            f"P&L: ${pnl:+.2f}\n\n"
            "Send /start to resume"
        )
        await self.send(msg)

    async def notify_daily_summary(
        self,
        trades: int,
        successful: int,
        partial: int,
        pnl: float,
        poly_balance: float,
        kalshi_balance: float,
    ) -> None:
        """Send end-of-day summary."""
        msg = self._format_daily_summary(
            trades, successful, partial, pnl, poly_balance, kalshi_balance
        )
        await self.send(msg)

    def _format_execution_message(
        self,
        result: ExecutionResult,
        event_title: str,
        roi: float,
        profit: float,
        poly_balance: float = 0,
        kalshi_balance: float = 0,
    ) -> str:
        """Format execution result as Telegram message."""
        if result.status == ExecutionStatus.SUCCESS:
            header = "✅ <b>EXECUTED</b> — Arbitrage locked in!"
            status_details = ""
        elif result.status == ExecutionStatus.ROLLED_BACK:
            header = "🔄 <b>ROLLED BACK</b> — Partial fill recovered"
            status_details = f"\n\n🔄 Rollback spread loss: ${result.rollback_loss:.2f}"
        elif result.status == ExecutionStatus.ROLLBACK_FAILED:
            header = "🚨 <b>ROLLBACK FAILED</b> — Manual action required!"
            status_details = f"\n\n🚨 Rollback error: {result.rollback_leg.error if result.rollback_leg else 'Unknown'}"
        elif result.status == ExecutionStatus.PARTIAL:
            header = "⚠️ <b>PARTIAL FILL</b> — Attention required!"
            failed_leg = result.kalshi_leg if not result.kalshi_leg.success else result.poly_leg
            status_details = f"\n\n🔴 {failed_leg.platform}: {failed_leg.error}"
        else:
            header = "❌ <b>FAILED</b> — Both legs failed"
            status_details = (
                f"\n\nPoly: {result.poly_leg.error}"
                f"\nKalshi: {result.kalshi_leg.error}"
            )

        msg = (
            f"{header}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏀 {event_title}\n"
        )

        # Show positions
        msg += "\n📊 <b>Positions:</b>"
        if result.poly_leg.success:
            msg += (
                f"\nPoly: ✅ {result.poly_leg.filled_shares:.2f} shares "
                f"× ${result.poly_leg.filled_price:.2f} = ${result.poly_leg.filled_cost:.2f}"
            )
        else:
            msg += f"\nPoly: ❌ {result.poly_leg.error or 'Failed'}"

        if result.kalshi_leg.success:
            msg += (
                f"\nKalshi: ✅ {int(result.kalshi_leg.filled_shares)} contracts "
                f"× ${result.kalshi_leg.filled_price:.2f} = ${result.kalshi_leg.filled_cost:.2f}"
            )
        else:
            msg += f"\nKalshi: ❌ {result.kalshi_leg.error or 'Failed'}"

        # Show payout for successful arbs
        if result.status == ExecutionStatus.SUCCESS:
            msg += (
                f"\n\n💰 <b>Payout:</b>"
                f"\nTotal invested: ${result.total_invested:.2f}"
                f"\nGuaranteed payout: ${result.guaranteed_payout:.2f}"
                f"\n📈 Profit: ${result.expected_profit:.2f} ({roi:.1f}% ROI)"
            )

        if status_details:
            msg += status_details

        if poly_balance > 0 or kalshi_balance > 0:
            msg += f"\n\n💳 Balances: Poly ${poly_balance:.2f} | Kalshi ${kalshi_balance:.2f}"

        return msg

    def _format_daily_summary(
        self,
        trades: int,
        successful: int,
        partial: int,
        pnl: float,
        poly_balance: float,
        kalshi_balance: float,
    ) -> str:
        """Format daily summary message."""
        success_rate = (successful / trades * 100) if trades > 0 else 0

        return (
            "📊 <b>DAILY SUMMARY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Trades: {trades}\n"
            f"Successful: {successful} ({success_rate:.0f}%)\n"
            f"Partial: {partial}\n\n"
            f"P&L: ${pnl:+.2f}\n"
            f"Balances: Poly ${poly_balance:.2f} | Kalshi ${kalshi_balance:.2f}"
        )

    async def setup_commands(self, executor) -> None:
        """Setup bot command handlers."""
        self._executor = executor

        self._app = Application.builder().token(self.bot_token).build()

        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop_commands(self) -> None:
        """Stop the bot command handlers."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        stats = self._executor.risk.get_stats()

        try:
            poly_bal = await self._executor.poly.get_balance()
            kalshi_bal = await self._executor.kalshi.get_balance()
        except Exception:
            poly_bal = 0
            kalshi_bal = 0

        positions = await self._executor.positions.get_open_positions()

        msg = (
            "📊 <b>STATUS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Enabled: {'✅' if stats['enabled'] else '❌'}\n"
            f"Daily trades: {stats['daily_trades']}/{stats['limits']['max_daily_trades']}\n"
            f"Daily P&L: ${stats['daily_pnl']:+.2f}\n"
            f"Open positions: {len(positions)}\n\n"
            f"💰 Balances:\n"
            f"  Poly: ${poly_bal:.2f}\n"
            f"  Kalshi: ${kalshi_bal:.2f}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stop command."""
        self._executor.risk.enabled = False
        await update.message.reply_text("🛑 Auto-trading STOPPED")

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        self._executor.risk.enabled = True
        await update.message.reply_text("✅ Auto-trading STARTED")

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /trades command - show recent trades."""
        positions = await self._executor.positions.get_open_positions()

        if not positions:
            await update.message.reply_text("No open positions")
            return

        msg = "📜 <b>OPEN POSITIONS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        for pos in positions[:10]:
            msg += f"• {pos.event_title}\n  ROI: {pos.expected_roi:.1f}%\n"

        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pnl command."""
        stats = await self._executor.positions.get_daily_stats()

        msg = (
            "💰 <b>P&L SUMMARY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Today: ${stats['pnl']:+.2f}\n"
            f"Trades: {stats['trades']}\n"
            f"Settled: {stats['settled']}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
