# Path to the Linux 7.3.1 tree with kernel/io_ext-linux-7.3.1.patch applied
KERNEL_SRC ?= $(abspath ../kernel/linux-7.3.1)
BUILD_DIR := build
IOEXT_DEBUG ?= 0

CLANG ?= clang
BPFTOOL ?= $(KERNEL_SRC)/tools/bpf/bpftool/bpftool
LIBBPF_A ?= $(KERNEL_SRC)/tools/lib/bpf/libbpf.a

ARCH ?= $(shell uname -m | sed 's/x86_64/x86/' \
			 | sed 's/arm.*/arm/' \
			 | sed 's/aarch64/arm64/' \
			 | sed 's/ppc64le/powerpc/' \
			 | sed 's/mips.*/mips/' \
			 | sed 's/riscv64/riscv/' \
			 | sed 's/loongarch64/loongarch/')

BPF_INCLUDES := -I$(BUILD_DIR) -I. \
		-I$(KERNEL_SRC)/tools/lib \
		-I$(KERNEL_SRC)/tools/include/uapi

BPF_CFLAGS := -g -O2 -target bpf -D__TARGET_ARCH_$(ARCH) \
	-DIOEXT_DEBUG=$(IOEXT_DEBUG) \
	$(BPF_INCLUDES) \
	-Wno-missing-declarations

USER_CFLAGS := -g -O2 -static \
	-DIOEXT_DEBUG=$(IOEXT_DEBUG) \
	$(BPF_INCLUDES)

USER_LDFLAGS := $(LIBBPF_A) -lelf -lz -lzstd

POLICIES := minimal q_fifo q_rfirst q_prio q_none q_noop q_accept_noop q_ddl q_db q_mq_deadline q_kyber q_bfq q_wedf q_sp_edf q_merge_probe q_heimdall q_heimdall_v2 q_ratelimit q_linnos q_iomax q_inject_null q_inject_stall
ARBITERS := arbiter arb_noop arb_sp arb_mix arb_bfq arb_wdrr arb_slo arb_cost arb_d2fq arb_feedback arb_iocost arb_iolatency
DAEMONS := heimdall_daemon
ALL_BPFS := $(POLICIES) $(ARBITERS)

BPF_OBJECTS := $(addprefix $(BUILD_DIR)/,$(addsuffix .bpf.o,$(ALL_BPFS)))
SKELETONS := $(addprefix $(BUILD_DIR)/,$(addsuffix .skel.h,$(ALL_BPFS)))
POLICY_LOADERS := $(foreach pol,$(POLICIES),$(if $(wildcard loader_$(pol).c),$(BUILD_DIR)/loader_$(pol)))
ARBITER_LOADERS := $(foreach arb,$(ARBITERS),$(if $(wildcard loader_$(arb).c),$(BUILD_DIR)/loader_$(arb)))
ALL_LOADERS := $(POLICY_LOADERS) $(ARBITER_LOADERS)
DAEMON_BINS := $(addprefix $(BUILD_DIR)/,$(DAEMONS))

VMLINUX_H := $(BUILD_DIR)/vmlinux.h
VMLINUX_BTF := $(shell if [ -f '$(KERNEL_SRC)/vmlinux' ]; then echo '$(KERNEL_SRC)/vmlinux'; else echo '$(KERNEL_SRC)/vmlinux.unstripped'; fi)

CLANG_BPF_SYS_INCLUDES ?= $(shell $(CLANG) -v -E - </dev/null 2>&1 \
	| sed -n '/<...> search starts here:/,/End of search list./{ s| \(/.*\)|-idirafter \1|p }')

.PHONY: all clean $(POLICIES) $(ARBITERS) $(DAEMONS)
.SECONDARY: $(BPF_OBJECTS) $(SKELETONS)

all: $(ALL_LOADERS) $(DAEMON_BINS)

$(POLICIES): %: $(BUILD_DIR)/loader_%
$(ARBITERS): %: $(BUILD_DIR)/loader_%
$(DAEMONS): %: $(BUILD_DIR)/%

$(BUILD_DIR):
	mkdir -p $@

$(VMLINUX_H): | $(BUILD_DIR)
	@test -f '$(VMLINUX_BTF)' || { echo "Need kernel vmlinux at $(VMLINUX_BTF); run ./scripts/make.sh first" >&2; exit 1; }
	$(BPFTOOL) btf dump file '$(VMLINUX_BTF)' format c > $@

$(BUILD_DIR)/%.bpf.o: %.bpf.c $(VMLINUX_H) ioext_bpf_common.h ioext_policy_shared.h ioext_arb_common.h | $(BUILD_DIR)
	$(CLANG) $(BPF_CFLAGS) $(CLANG_BPF_SYS_INCLUDES) -c $< -o $@

$(BUILD_DIR)/%.skel.h: $(BUILD_DIR)/%.bpf.o | $(BUILD_DIR)
	$(BPFTOOL) gen skeleton $< > $@

$(BUILD_DIR)/loader_%: loader_%.c $(BUILD_DIR)/%.skel.h | $(BUILD_DIR)
	$(CLANG) $(USER_CFLAGS) $< -o $@ $(USER_LDFLAGS)

$(BUILD_DIR)/heimdall_daemon: heimdall_daemon.c ioext_loader_common.h ioext_policy_shared.h | $(BUILD_DIR)
	$(CLANG) $(USER_CFLAGS) $< -o $@ $(USER_LDFLAGS)

clean:
	rm -rf $(BUILD_DIR)

# ---------------------------------------------------------------------------
# D1 experiment E6: standalone fentry/fexit instrumentation of the ioext3
# core callbacks (scx_io_insert_requests / scx_io_dispatch_request).  Not
# part of the policy/arbiter ladder (no contract maps), so it is built on
# demand only, exactly like the q_noop/arb_noop targets above.
D1_INSTR := d1_instr

# Keep the BPF object around for offline inspection (llvm-objdump -h).
.SECONDARY: $(BUILD_DIR)/d1_instr.bpf.o $(BUILD_DIR)/d1_instr.skel.h

.PHONY: $(D1_INSTR)
$(D1_INSTR): %: $(BUILD_DIR)/loader_%
