/* loader_d1_instr.c - loader for the D1 E6 fentry/fexit instrumentation.
 *
 * Takes no CLI arguments.  Lifecycle modelled on loader_arb_noop: open ->
 * load -> attach all four fentry/fexit programs (each failure reported
 * explicitly, exit non-zero) -> pin the segs map to /sys/fs/bpf/d1_instr_segs
 * (stale node unlinked first) -> print the "d1_instr: attached" marker and
 * pause until SIGTERM/SIGINT, then unpin and destroy.  The analyzer reads
 * the segs map with
 *
 *   bpftool -j map dump pinned /sys/fs/bpf/d1_instr_segs
 *
 * BEFORE killing the loader.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <bpf/libbpf.h>

#include "d1_instr.skel.h"

#define D1_PIN_SEGS "/sys/fs/bpf/d1_instr_segs"

static volatile sig_atomic_t d1_stop;

static void d1_on_signal(int sig)
{
	(void)sig;
	d1_stop = 1;
}

static int d1_attach(struct d1_instr_bpf *skel, const char *what,
		     struct bpf_program *prog, struct bpf_link **slot)
{
	struct bpf_link *link;

	link = bpf_program__attach(prog);
	if (!link) {
		fprintf(stderr, "loader_d1_instr: failed to attach %s: %s\n",
			what, strerror(errno));
		return -1;
	}
	/* Ownership moved into the skeleton: destroy detaches it. */
	*slot = link;
	return 0;
}

int main(void)
{
	struct sigaction sa = { .sa_handler = d1_on_signal };
	struct d1_instr_bpf *skel;
	int ret = 1;

	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);

	skel = d1_instr_bpf__open();
	if (!skel) {
		fprintf(stderr, "loader_d1_instr: failed to open skeleton\n");
		return 1;
	}

	if (d1_instr_bpf__load(skel)) {
		fprintf(stderr, "loader_d1_instr: failed to load BPF skeleton\n");
		goto out;
	}

	if (d1_attach(skel, "fentry/scx_io_insert_requests",
		      skel->progs.d1_insert_entry,
		      &skel->links.d1_insert_entry))
		goto out;
	if (d1_attach(skel, "fexit/scx_io_insert_requests",
		      skel->progs.d1_insert_exit,
		      &skel->links.d1_insert_exit))
		goto out;
	if (d1_attach(skel, "fentry/scx_io_dispatch_request",
		      skel->progs.d1_dispatch_entry,
		      &skel->links.d1_dispatch_entry))
		goto out;
	if (d1_attach(skel, "fexit/scx_io_dispatch_request",
		      skel->progs.d1_dispatch_exit,
		      &skel->links.d1_dispatch_exit))
		goto out;

	/* Publish the stats map at a fixed path for the analyzer; drop any
	 * stale node left behind by a previous loader run. */
	unlink(D1_PIN_SEGS);
	if (bpf_map__pin(skel->maps.segs, D1_PIN_SEGS)) {
		fprintf(stderr, "loader_d1_instr: failed to pin segs map to %s: %s\n",
			D1_PIN_SEGS, strerror(errno));
		goto out;
	}

	printf("d1_instr: attached\n");
	fflush(stdout);

	while (!d1_stop)
		pause();

	ret = 0;
out:
	unlink(D1_PIN_SEGS);
	d1_instr_bpf__destroy(skel);
	return ret;
}
