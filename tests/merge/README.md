# io-ext Merge 测试套件

> **设计文档**：[`userspace/MERGE.md`](../../userspace/MERGE.md)  
> **策略百科**：[`userspace/IO_SCHED_POLICIES.md`](../../userspace/IO_SCHED_POLICIES.md)

---

## 快速开始

```bash
cd ioext

# 完整流程：构建 → 启动 virtme-ng VM → 跑 suite → 生成报告
./tests/merge/run_on_virtme.sh

# 跳过构建（内核与 loader 已就绪）
./tests/merge/run_on_virtme.sh --skip-build --fio-runtime 30

# 只跑某一 phase
./tests/merge/run_on_virtme.sh --phase 1
```

结果目录：`results/io_merge_suite/suite_<timestamp>/`

---

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `FIO_RUNTIME` | `30` | 每个 fio job 运行秒数；正式压测建议 `120–300` |
| `MERGE_PHASE` | `all` | `1` / `2` / `3` / `all` |
| `SKIP_BUILD` | `0` | `run_on_virtme.sh --skip-build` 时设为 `1` |
| `KERNEL_CONFIG` | `kernel/config/config-7.0-osext-merge-test` | merge 测试内核配置 |
| `IOEXT_DEBUG` | `1` | 启用 merge hook `noinline` |
| `OSEXT_VM_CPUS` | `64` | VM vCPU 数 |
| `OSEXT_VM_MEMORY` | `4G` | VM 内存 |
| `OSEXT_VM_DISK_IMG` | `test_disk.img` | NVMe 测试盘镜像 |
| `BASE_DIR` | 自动检测 | ioext 根目录 |

---

## 脚本说明

| 文件 | 运行位置 | 作用 |
|------|----------|------|
| `run_on_virtme.sh` | 宿主机 | 构建、启动 VM、触发 suite、汇总报告 |
| `run_merge_suite.sh` | VM 内 | 遍历 case、加载 policy、跑 fio、写 verdict |
| `gen_fio_job.sh` | VM 内 | 生成高压力 fio job 文件 |
| `collect_merge_evidence.sh` | VM 内 | 采证：env、merge_stats、bpftrace、dmesg |
| `bpftrace_merge.bt` | VM 内 | merge hook 符号探测 |
| `summarize_merge_results.py` | 宿主机 / VM | 生成 `MERGE_REPORT.md` |

---

## 采证目录结构（每 case）

```
results/io_merge_suite/suite_<timestamp>/<case_id>/
├── manifest.json
├── env.txt
├── verdict.json
├── merge_stats.json          # bpftool dump（probe / mq_deadline）
├── fio_L1_seq_write_heavy.json
├── fio_L3_rand_write_heavy.json   # 部分 case
├── fio_L6_low_qd_regress.json     # D* case
├── obs_status.txt
├── bpftrace_merge.log
├── dmesg_before.txt / dmesg_after.txt
├── mq_sysfs.txt
├── trace_snapshot.txt
└── loader_<policy>.log
```

Suite 级产物：

- `MERGE_REPORT.md` — 自动摘要（`summarize_merge_results.py`）
- `IMPLEMENTATION_REPORT.md` — 实现与测试完整报告（手工维护 / 归档）
- `run_on_virtme.log` — 宿主机执行日志

---

## VM 内手动调试

```bash
# 在 VM 内（virtme-ng shell 或 SSH）
cd /path/to/ioext
bash tests/merge/run_merge_suite.sh

# 单独加载探针（mode 1 = ALLOW_ALL）
userspace/build/loader_q_merge_probe /sys/fs/cgroup 0 100 1

# 读取统计
bpftool map dump pinned /sys/fs/bpf/merge_stats
```

---

## 判定标准

- **PASS**：fio 正常结束，无 hung_task / Oops，观测层 `OBS_PROBE_OK`
- merge 是否生效：以 BPF `merge_stats` 中 `bio_merge_true` / `allow_merge_true` 为准，**不要**单独依赖 `total_ios` 变化

详细结论见最新 suite 的 `IMPLEMENTATION_REPORT.md`。
