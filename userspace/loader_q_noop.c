/* loader_q_noop.c - ioext3 loader for the q_noop no-op queue policy.
 *
 * CLI contract identical to the other policy loaders (-c <cgroup> -p <prio>
 * -W <weight>; prio/weight are accepted and ignored by the policy itself)
 * so the D1' probe scripts can start it exactly like loader_q_fifo.
 */
#define _GNU_SOURCE

#include <stdio.h>

#include "q_noop.skel.h"
#include "ioext_loader_common.h"
#include "ioext_cli_args.h"

int main(int argc, char **argv)
{
	struct q_noop_bpf *skel;
	struct ioext_policy_args args;
	__u64 owner_token;
	int ret;

	ioext_cli_set_usage("q_noop", NULL);
	if (ioext_cli_parse_policy_args(argc, argv, &args, NULL, NULL)) {
		fprintf(stderr, "loader_q_noop: failed to parse args\n");
		return 1;
	}
	owner_token = iosched_loader_owner_token();

	skel = q_noop_bpf__open();
	if (!skel) {
		fprintf(stderr, "loader_q_noop: failed to open skeleton\n");
		return 1;
	}

	if (iosched_loader_setup_policy_maps(skel->obj)) {
		fprintf(stderr, "loader_q_noop: setup_policy_maps failed\n");
		q_noop_bpf__destroy(skel);
		return 1;
	}

	bpf_map__set_map_flags(skel->maps.scx_q_noop, BPF_F_LINK);
	if (q_noop_bpf__load(skel)) {
		fprintf(stderr, "loader_q_noop: failed to load BPF skeleton\n");
		q_noop_bpf__destroy(skel);
		return 1;
	}

	ret = iosched_loader_register_policy(args.cgroup, args.arb_prio,
					     args.arb_weight, owner_token);
	if (ret) {
		fprintf(stderr, "loader_q_noop: register_policy failed: %d\n", ret);
		q_noop_bpf__destroy(skel);
		return 1;
	}

	ret = iosched_loader_policy_attach_and_run(skel->obj,
						   skel->maps.scx_q_noop,
						   "q_noop",
						   args.cgroup,
						   owner_token);
	if (ret)
		fprintf(stderr, "loader_q_noop: attach_and_run returned %d\n", ret);
	q_noop_bpf__destroy(skel);
	return ret;
}
