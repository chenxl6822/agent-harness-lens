# harness_scope Makefile — 便利别名薄壳（真实现见 package.json scripts + scripts/）
# 构建入口以 `npm run *` 为准；本机无 make 时用 mingw32-make 执行本文件。
.DEFAULT_GOAL := help

.PHONY: gen check-gen lint typecheck test smoke help

gen:
	npm run gen

check-gen:
	npm run check-gen

lint:
	npm run lint

typecheck:
	npm run typecheck

test:
	npm run test

smoke:
	npm run smoke

help:
	@echo "harness_scope targets（真实现走 npm run *，此处仅别名）:"
	@echo "  make gen         = npm run gen         生成 TS/Python 契约类型（幂等）"
	@echo "  make check-gen   = npm run check-gen   校验生成产物未过期/未手改"
	@echo "  make lint        = npm run lint        ruff 检查"
	@echo "  make typecheck   = npm run typecheck   tsc strict 检查"
	@echo "  make test        = npm run test        pytest 单测"
	@echo "  make smoke       = npm run smoke       CLI 冒烟"
	@echo "  make help        本帮助"
