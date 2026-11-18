#ifndef IOEXT_BPF_EXPERIMENTAL_H
#define IOEXT_BPF_EXPERIMENTAL_H

#include <bpf/bpf_core_read.h>

/*
 * Local wrappers for BPF allocator / graph kfuncs.
 *
 * Prototypes are declared here because bpftool-generated vmlinux.h only
 * exposes KF_* enum tags, not callable kfunc signatures.
 */
#define __contains(name, node) \
    __attribute__((btf_decl_tag("contains:" #name ":" #node)))

extern void *bpf_obj_new_impl(__u64 local_type_id, void *meta) __ksym;
extern void bpf_obj_drop_impl(void *kptr, void *meta) __ksym;
extern void *bpf_refcount_acquire_impl(void *kptr, void *meta) __ksym;

extern int bpf_list_push_front_impl(struct bpf_list_head *head,
				    struct bpf_list_node *node,
				    void *meta, __u64 off) __ksym;
extern int bpf_list_push_back_impl(struct bpf_list_head *head,
				   struct bpf_list_node *node,
				   void *meta, __u64 off) __ksym;
extern struct bpf_list_node *bpf_list_front(struct bpf_list_head *head) __ksym;
extern struct bpf_list_node *bpf_list_pop_front(struct bpf_list_head *head) __ksym;

extern int bpf_rbtree_add_impl(struct bpf_rb_root *root, struct bpf_rb_node *node,
			       bool (less)(struct bpf_rb_node *a, const struct bpf_rb_node *b),
			       void *meta, __u64 off) __ksym;
extern struct bpf_rb_node *bpf_rbtree_remove(struct bpf_rb_root *root,
					     struct bpf_rb_node *node) __ksym;
extern struct bpf_rb_node *bpf_rbtree_root(struct bpf_rb_root *root) __ksym;
extern struct bpf_rb_node *bpf_rbtree_left(struct bpf_rb_root *root,
					   struct bpf_rb_node *node) __ksym;
extern struct bpf_rb_node *bpf_rbtree_right(struct bpf_rb_root *root,
					    struct bpf_rb_node *node) __ksym;

#define bpf_obj_new(type) \
    ((type *)bpf_obj_new_impl(bpf_core_type_id_local(type), NULL))
#define bpf_obj_drop(kptr) \
    bpf_obj_drop_impl(kptr, NULL)
#define bpf_refcount_acquire(kptr) \
    bpf_refcount_acquire_impl(kptr, NULL)

#define bpf_list_push_front(head, node) \
    bpf_list_push_front_impl(head, node, NULL, 0)
#define bpf_list_push_back(head, node) \
    bpf_list_push_back_impl(head, node, NULL, 0)

#define bpf_rbtree_add(root, node, less) \
    bpf_rbtree_add_impl(root, node, less, NULL, 0)

#endif
