SHELL := /usr/bin/env bash

.PHONY: check-env build run-base run-network run-town01 stop test-changed status \
	demo-preflight demo-town01 demo-rugged demo-stop

DEMO_SCENARIO ?= town01
DEMO_GUI ?= 1
DEMO_BOOTSTRAP ?= 0

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

demo-preflight:
	./scripts/product/native_demo_preflight.sh --scenario "$(DEMO_SCENARIO)" $(if $(filter 1,$(DEMO_GUI)),--gui) $(if $(filter 1,$(DEMO_BOOTSTRAP)),--bootstrap)

demo-town01:
	./scripts/product/native_demo_preflight.sh --scenario town01 $(if $(filter 1,$(DEMO_GUI)),--gui) $(if $(filter 1,$(DEMO_BOOTSTRAP)),--bootstrap)
	BAS_NATIVE_FIVE_SCENARIO=town01 BAS_NATIVE_FIVE_GUI="$(DEMO_GUI)" \
		./network/ns3/run_native_radio_five_uav.sh

demo-rugged:
	./scripts/product/native_demo_preflight.sh --scenario rock_demo $(if $(filter 1,$(DEMO_GUI)),--gui) $(if $(filter 1,$(DEMO_BOOTSTRAP)),--bootstrap)
	BAS_NATIVE_FIVE_SCENARIO=rock_demo BAS_NATIVE_FIVE_GUI="$(DEMO_GUI)" \
		./network/ns3/run_native_radio_five_uav.sh

demo-stop:
	./scripts/product/stop_all.sh
