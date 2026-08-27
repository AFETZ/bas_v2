SHELL := /usr/bin/env bash

.PHONY: check-env build run-base run-network run-town01 stop test-changed status

check-env:
	./scripts/product/check_env.sh

build:
	colcon build --symlink-install

run-base:
	./scripts/product/run_5uav_base.sh

run-network:
	./scripts/product/run_network.sh

run-town01:
	./scripts/product/run_town01_full_stack.sh

stop:
	./scripts/product/stop_all.sh

test-changed:
	./scripts/product/test_changed.sh

status:
	@sed -n '1,150p' network/STATUS.md
