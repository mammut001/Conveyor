# codex-telegram-runner local Makefile
#
# make smoke       runs the env-free AST/behavior smokes. Use it as a pre-deploy
#                  gate before 'bash scripts/deploy.sh'. memo_smoke needs a populated
#                  .env and is gated behind 'make smoke-all' for that reason.

PY ?= .venv/bin/python
SMOKE_FREE = scripts/auto_maintain_smoke.py scripts/compress_day_smoke.py scripts/clean_worktrees_smoke.py

.PHONY: smoke smoke-all help

help:
	@echo 'make smoke        run env-free smokes (pre-deploy gate)'
	@echo 'make smoke-all    also run memo_smoke (requires .env)'

smoke:
	@set -e; for s in $(SMOKE_FREE); do echo '>>>' $$s; $(PY) $$s; done

smoke-all: smoke
	@echo '>>> scripts/memo_smoke.py (requires .env)'
	@$(PY) scripts/memo_smoke.py
