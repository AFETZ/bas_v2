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

run-network: demo-town01

run-town01: demo-town01

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

.PHONY: prepare-customer demo-customer operator gcs native-sources native-maps native-cache-study native-matrix
OPERATOR_DURATION ?= 900

prepare-customer:
	./scripts/product/prepare_customer_scene.sh

demo-customer:
	BAS_NATIVE_FIVE_SCENARIO=customer BAS_NATIVE_FIVE_GUI="$(DEMO_GUI)" ./network/ns3/run_native_radio_five_uav.sh

operator:
	BAS_NATIVE_FIVE_SCENARIO="$(DEMO_SCENARIO)" BAS_NATIVE_OPERATOR_SECONDS="$(OPERATOR_DURATION)" BAS_NATIVE_FIVE_GUI="$(DEMO_GUI)" ./network/ns3/run_native_radio_five_uav.sh

gcs:
	docker exec -it bas-v2-native-radio-five-uav ip netns exec ams-gcs /home/ubuntu/.local/bin/mavproxy.py --master=udpout:127.0.0.1:14551 --master=udpout:127.0.0.1:14552 --master=udpout:127.0.0.1:14553 --master=udpout:127.0.0.1:14554 --master=udpout:127.0.0.1:14555 --aircraft=/workspace/multiagent_simulation/runs/operator

native-sources:
	BAS_NATIVE_SOURCES_CAMPAIGN=1 ./network/ns3/run_sionna_wifi_five_uav.sh

native-maps:
	BAS_NATIVE_STUDY=maps ./network/ns3/run_sionna_wifi_five_uav.sh

native-cache-study:
	BAS_NATIVE_STUDY=cache ./network/ns3/run_sionna_wifi_five_uav.sh

native-matrix:
	BAS_NATIVE_STUDY=matrix ./network/ns3/run_sionna_wifi_five_uav.sh

.PHONY: demo-record demo-record-all demo-video
SCENARIO ?= 01
DEMO_OUTPUT ?= /home/bas/bas_v2-demo/rc1-2026-09-05

demo-record:
	python3 scripts/product/demo_record.py --scenario "$(SCENARIO)" --output "$(DEMO_OUTPUT)"

demo-record-all:
	python3 scripts/product/demo_record.py --scenario all --output "$(DEMO_OUTPUT)"

demo-video:
	python3 scripts/product/demo_prepare_video.py --output "$(DEMO_OUTPUT)"
	python3 scripts/product/demo_video.py --output "$(DEMO_OUTPUT)"
	python3 scripts/product/demo_finish_video.py --output "$(DEMO_OUTPUT)"
