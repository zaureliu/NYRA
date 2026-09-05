"""Pure relevance and interruption policy for Proactive Presence V1."""

from __future__ import annotations

from app.proactive_presence.models import (
    DecisionContext,
    DecisionRecord,
    ProactiveCandidate,
    ProactiveDecision,
    ProactiveMode,
    ProactivePriority,
    ProactiveSettings,
)


_BUSY_ASSISTANT = {"THINKING", "ACTING", "SPEAKING", "LISTENING"}
_BUSY_USER = {"SPEAKING", "LISTENING"}


class ProactiveDecisionEngine:
    """Scores grounded candidates and chooses presentation, never execution."""

    def decide(
        self,
        candidate: ProactiveCandidate,
        context: DecisionContext,
        settings: ProactiveSettings,
    ) -> DecisionRecord:
        score = self.score(candidate, context)
        decision, reason = self._decision(candidate, context, settings, score)
        return DecisionRecord(
            event_id=candidate.event_id,
            event_type=candidate.event_type,
            source=candidate.source,
            entity=candidate.entity,
            goal_id=candidate.goal_id,
            open_loop_id=candidate.open_loop_id,
            priority=candidate.priority,
            score=score,
            decision=decision,
            reason=reason,
            repeat_count=context.repeat_count,
            dedup_key=candidate.dedup_key,
            execution_authorized=False,
            action_budget_consumed=0,
        )

    @staticmethod
    def score(candidate: ProactiveCandidate, context: DecisionContext) -> float:
        repeat_signal = min(context.repeat_count / 5, 1.0)
        value = 100 * (
            .25 * candidate.impact
            + .20 * candidate.urgency
            + .18 * context.relation_to_active_goal
            + .12 * context.relation_to_recent_request
            + .10 * context.novelty
            + .05 * repeat_signal
            + .07 * candidate.confidence
            + .03 * context.freshness
        )
        priority_floor = {
            ProactivePriority.LOW: 0,
            ProactivePriority.NORMAL: 48,
            ProactivePriority.HIGH: 68,
            ProactivePriority.CRITICAL: 92,
        }[candidate.priority]
        return round(min(100.0, max(value, priority_floor)), 2)

    @staticmethod
    def _decision(
        candidate: ProactiveCandidate,
        context: DecisionContext,
        settings: ProactiveSettings,
        score: float,
    ) -> tuple[ProactiveDecision, str]:
        if candidate.baseline == "IGNORE":
            return ProactiveDecision.IGNORE, "policy_irrelevant"
        if candidate.baseline == "LOG_ONLY":
            return ProactiveDecision.LOG_ONLY, "policy_audit_only"
        if not settings.enabled:
            return ProactiveDecision.LOG_ONLY, "presence_disabled"
        if candidate.recovery_of and not context.recovery_relevant:
            return ProactiveDecision.LOG_ONLY, "recovery_without_notified_incident"
        if context.cooldown_active:
            return ProactiveDecision.LOG_ONLY, "semantic_cooldown_coalesced"
        if score < 32:
            return ProactiveDecision.IGNORE, "relevance_below_ignore_threshold"
        if score < 48:
            return ProactiveDecision.LOG_ONLY, "relevance_below_notification_threshold"

        priority = candidate.priority
        if settings.mode == ProactiveMode.DO_NOT_DISTURB and priority != ProactivePriority.CRITICAL:
            return ProactiveDecision.LOG_ONLY, "do_not_disturb_noncritical"
        if settings.mode == ProactiveMode.QUIET and priority.rank < ProactivePriority.HIGH.rank:
            return ProactiveDecision.LOG_ONLY, "quiet_mode_below_high"
        if (
            context.notifications_last_hour >= settings.max_notifications_per_hour
            and priority.rank < ProactivePriority.HIGH.rank
        ):
            return ProactiveDecision.DEFER, "hourly_presentation_budget"

        assistant = context.assistant_state.upper()
        user = context.user_activity.upper()
        busy = assistant in _BUSY_ASSISTANT or user in _BUSY_USER
        if busy and priority == ProactivePriority.CRITICAL:
            return ProactiveDecision.CHAT_MESSAGE, "critical_bypass_without_voice_overlap"
        if busy and priority.rank < ProactivePriority.CRITICAL.rank:
            return ProactiveDecision.DEFER, "assistant_or_user_busy"

        if (
            settings.mode == ProactiveMode.NORMAL
            and settings.voice_enabled
            and context.voice_ready
            and priority.rank >= ProactivePriority.HIGH.rank
        ):
            return ProactiveDecision.VOICE_AND_CHAT, "high_relevance_voice_available"
        if priority.rank >= ProactivePriority.HIGH.rank:
            return ProactiveDecision.CHAT_MESSAGE, "high_priority_persistent_chat"
        if user in {"IDLE", "AWAY"}:
            return ProactiveDecision.CHAT_MESSAGE, "relevant_while_user_not_active"
        return ProactiveDecision.UI_NOTIFICATION, "active_user_nonintrusive_ui"
