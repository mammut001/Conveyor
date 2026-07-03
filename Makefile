# Conveyor local Makefile
#
# make smoke       runs the env-free AST/behavior smokes. Use it as a pre-deploy
#                  gate before 'bash scripts/deploy.sh'. memo_smoke needs a populated
#                  .env and is gated behind 'make smoke-all' for that reason.

PY ?= .venv/bin/python
SMOKE_FREE = scripts/auto_maintain_smoke.py scripts/desktop_observe_request_smoke.py scripts/compress_day_smoke.py scripts/clean_worktrees_smoke.py scripts/clean_old_jobs_smoke.py scripts/classify_memo_smoke.py scripts/memo_flow_smoke.py scripts/memo_fastpath_smoke.py scripts/progress_smoke.py scripts/handlers_smoke.py scripts/jobs_dedupe_smoke.py scripts/jobs_progress_mode_smoke.py scripts/ops_intent_smoke.py scripts/ops_smoke.py scripts/ops_run_smoke.py scripts/telegram_outbound_smoke.py scripts/tools_intent_smoke.py scripts/tools_runner_smoke.py scripts/telegram_command_fallback_smoke.py scripts/confirm_strict_smoke.py scripts/ps_full_smoke.py scripts/diagnose_command_smoke.py scripts/restart_alias_smoke.py scripts/tools_output_smoke.py scripts/confirmation_context_smoke.py scripts/tool_audit_smoke.py scripts/audit_tools_smoke.py scripts/telegram_live_helpers_smoke.py scripts/docs_consistency_smoke.py scripts/channel_telegram_smoke.py scripts/channel_feishu_smoke.py scripts/feishu_cards_smoke.py scripts/import_boundary_smoke.py scripts/deploy_workflow_smoke.py scripts/deploy_status_smoke.py scripts/deploy_verify_p5_2.py scripts/session_summary_smoke.py scripts/audit_rotation_smoke.py scripts/personal_tools_smoke.py scripts/scheduler_probe_smoke.py scripts/gmail_smoke.py scripts/google_tools_smoke.py scripts/briefing_smoke.py scripts/github_smoke.py scripts/planner_smoke.py scripts/job_queue_smoke.py scripts/project_profiles_smoke.py scripts/setup_smoke.py scripts/project_io_smoke.py scripts/web_tools_smoke.py scripts/research_smoke.py scripts/file_search_smoke.py scripts/nl_router_smoke.py scripts/nodes_smoke.py scripts/desktop_agent_protocol_smoke.py scripts/desktop_screenshot_smoke.py scripts/worktree_isolation_smoke.py scripts/apply_policy_smoke.py scripts/session_lock_smoke.py scripts/redaction_policy_smoke.py scripts/security_audit_smoke.py scripts/quota_smoke.py scripts/security_regression_smoke.py

.PHONY: smoke smoke-all help

help:
	@echo 'make smoke        run env-free smokes (pre-deploy gate)'
	@echo 'make smoke-all    also run memo_smoke (requires .env)'

smoke:
	@set -e; for s in $(SMOKE_FREE); do echo '>>>' $$s; $(PY) $$s; done

smoke-all: smoke
	@echo '>>> scripts/memo_smoke.py (requires .env)'
	@$(PY) scripts/memo_smoke.py
