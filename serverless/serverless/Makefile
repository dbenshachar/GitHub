# Run from this repository. All cluster commands use kubectl inside Lima.
.DEFAULT_GOAL := help
PROFILE ?= standalone
VM ?= fc
ACCEL ?= auto
POOL_MIN ?= 8
POOL_MAX ?= 64
CALIBRATE ?= 100
QUALIFY_USERS ?= 1500
VERBOSE ?= 0
export PROFILE VM ACCEL POOL_MIN POOL_MAX CALIBRATE QUALIFY_USERS VERBOSE
PYTHON ?= python3
MINI_OS ?= ../../mini_os
ifeq ($(PROFILE),kubernetes-single)
CONTROL ?= fc
NODES ?= fc
SERVERS ?= 1
KUBE_MANIFEST ?= deploy/kubernetes-single.yaml
else
CONTROL ?= mini-control
SERVERS ?= 5
KUBE_MANIFEST ?= deploy/kubernetes.yaml
endif
export SERVERS KUBE_MANIFEST
NODES ?= mini-control mini-worker-1 mini-worker-2 mini-worker-3 mini-worker-4
STATE_DIR ?= .load-test
ROUTER_PORT ?= 8080
MEMORY ?= 8
CPUS ?= 0.25
JOB_TIMEOUT ?= 30
START_USERS ?= 4096
TARGET_USERS ?= 1000000
REQUESTS_PER_MINUTE ?= 1
DURATION ?= 15
MAX_INFLIGHT ?= 8192
MAX_ERROR_FRACTION ?= 0.01
REQUEST_TIMEOUT ?= 40
OUTPUT ?= load-go.json
SCRIPT ?= examples/HELLO.MOS
export PYTHON MINI_OS CONTROL NODES STATE_DIR ROUTER_PORT
export MEMORY CPUS JOB_TIMEOUT START_USERS TARGET_USERS
export REQUESTS_PER_MINUTE DURATION MAX_INFLIGHT MAX_ERROR_FRACTION REQUEST_TIMEOUT OUTPUT SCRIPT

.PHONY: help bootstrap image deploy router server load test qualify stop logs unit-test go-test smoke
help:
	@printf '%s\n' 'make bootstrap  Prepare selected Lima VM (restarts it; installs guest dependencies)' 'Default: standalone Go service on VM=fc (4 CPUs, 8GiB, vzNAT)' 'make qualify    Three 600s runs, p95 <1s and errors <1% (QUALIFY_USERS=1500)' 'PROFILE=kubernetes-single deploys the combined service to one K3s node' 'PROFILE=kubernetes retains the five-node cluster workflow' 'make image      Build only when runtime/build source changes' 'make deploy     Start background service (cluster deployment with PROFILE=kubernetes)' 'make router     Start combined standalone router/service' 'make server     Start Go supervisor and unused-guest pool' 'make load       Ramp 4096 to 1M users, display and save metrics' 'make test       Build, start background service, run exploratory ramp' 'make stop       Stop background processes started by this Makefile' 'make logs       Show background logs' 'make unit-test  Run Python unit tests' 'Overrides: make test VM=fc DURATION=15; make test PROFILE=kubernetes'

bootstrap:
	@$(PYTHON) -m tools.standalone_setup bootstrap

image deploy router server load test stop logs:
ifeq ($(PROFILE),standalone)
	@$(PYTHON) -m tools.standalone_setup $@
else
	@$(PYTHON) -m tools.load_test_setup $@
endif

qualify:
	@$(PYTHON) -m tools.standalone_setup qualify

smoke:
	@$(PYTHON) -m tools.smoke_go

go-test:
	@GOCACHE="$(CURDIR)/.go-cache" go test -race ./...

unit-test:
	@$(PYTHON) -m unittest discover -s tests -v
