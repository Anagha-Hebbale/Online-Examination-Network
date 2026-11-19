import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import mn_common as mc  # noqa: E402

SERIES_ORDER = ["static/baseline", "static/flood", "dynamic/baseline", "dynamic/flood"]
SERIES_LABEL = {"static/baseline": "static, no flood", "static/flood": "static, flood",
                "dynamic/baseline": "dynamic, no flood", "dynamic/flood": "dynamic, flood"}
SERIES_COLOR = {"static/baseline": "#7f7f7f", "static/flood": "#D55E00",
                "dynamic/baseline": "#56B4E9", "dynamic/flood": "#009E73"}


def load_trials(folders):
    trials = []
    for folder in folders:
        for meta_path in sorted(Path(folder).glob("*/meta.json")):
            meta = json.loads(meta_path.read_text())
            rows = mc.load_samples(meta_path.parent, "load")
            events = mc.read_events(meta_path.parent / "controller_events.jsonl")
            start = meta["flood_start"] or (meta["t_load"] + meta["flood_at"])
            stats = mc.window_stats(rows, start, meta["t_end"])
            mitigation = next((e for e in events if e["event"] == "MITIGATION_INSTALLED"), None)
            record = {"series": f"{meta['mode']}/{meta['condition']}", "mode": meta["mode"],
                      "condition": meta["condition"], "clients": meta["clients"], "rep": meta["rep"],
                      "window_s": stats["seconds"], "rtt_mean_ms": stats["rtt_mean_ms"],
                      "rtt_p50_ms": stats["rtt_p50_ms"], "rtt_p95_ms": stats["rtt_p95_ms"],
                      "exams_per_s": stats["exams_per_s"], "exams_ok": stats["exams_ok"],
                      "exams_failed": stats["exams_failed"],
                      "success_rate": stats["success_rate"],
                      "seconds_to_mitigate": round(mitigation["ts"] - start, 2) if mitigation and meta["flood_start"] else None}
            if mitigation and meta["flood_start"]:
                post = mc.window_stats(rows, mitigation["ts"] + 2, meta["t_end"])
                record["rtt_mean_after_mitigation_ms"] = post["rtt_mean_ms"]
            else:
                record["rtt_mean_after_mitigation_ms"] = None
            trials.append({"record": record, "meta": meta, "rows": rows, "events": events, "start": start})
    return trials


def mean_std(values):
    values = [v for v in values if v is not None]
    if not values:
        return None, None
    return statistics.mean(values), (statistics.stdev(values) if len(values) > 1 else 0.0)


METRICS = ["rtt_mean_ms", "rtt_p95_ms", "exams_per_s", "success_rate", "seconds_to_mitigate",
           "rtt_mean_after_mitigation_ms"]


def aggregate(trials):
    groups = defaultdict(list)
    for t in trials:
        groups[(t["record"]["series"], t["record"]["clients"])].append(t["record"])
    out = []
    for (series, clients), recs in sorted(groups.items(), key=lambda kv: (SERIES_ORDER.index(kv[0][0]) if kv[0][0] in SERIES_ORDER else 9, kv[0][1])):
        row = {"series": series, "clients": clients, "trials": len(recs),
               "exams_failed_total": sum(r["exams_failed"] for r in recs)}
        for m in METRICS:
            mean, std = mean_std([r[m] for r in recs])
            row[m] = round(mean, 3) if mean is not None else None
            row[m + "_std"] = round(std, 3) if std is not None else None
        out.append(row)
    return out


def write_csv(path, rows):
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fmt(value, std=None, digits=1, pct=False):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    text = f"{value:.{digits}f}"
    return text + (f" ± {std:.{digits}f}" if std else "")


def write_markdown(path, agg):
    lines = ["| Controller / traffic | Clients | Trials | Mean RTT (ms) | p95 RTT (ms) | Exams/s | Success | Time to mitigate (s) |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in agg:
        lines.append(f"| {SERIES_LABEL.get(r['series'], r['series'])} | {r['clients']} | {r['trials']} | "
                     f"{fmt(r['rtt_mean_ms'], r['rtt_mean_ms_std'])} | {fmt(r['rtt_p95_ms'], r['rtt_p95_ms_std'])} | "
                     f"{fmt(r['exams_per_s'], r['exams_per_s_std'])} | {fmt(r['success_rate'], pct=True)} | "
                     f"{fmt(r['seconds_to_mitigate'], r['seconds_to_mitigate_std'])} |")
    Path(path).write_text("\n".join(lines) + "\n\nValues are mean ± standard deviation over repetitions.\n")


def bar_chart(agg, metric, ylabel, title, path, log=False, percent=False):
    clients = sorted({r["clients"] for r in agg})
    series = [s for s in SERIES_ORDER if any(r["series"] == s for r in agg)]
    if not clients or not series:
        return
    fig, ax = plt.subplots(figsize=(8, 4.5))
    width = 0.8 / len(series)
    for i, s in enumerate(series):
        xs, ys, errs = [], [], []
        for j, c in enumerate(clients):
            row = next((r for r in agg if r["series"] == s and r["clients"] == c), None)
            if row and row[metric] is not None:
                xs.append(j + (i - (len(series) - 1) / 2) * width)
                ys.append(row[metric] * (100 if percent else 1))
                errs.append((row[metric + "_std"] or 0) * (100 if percent else 1))
        ax.bar(xs, ys, width * 0.92, yerr=errs, capsize=2, label=SERIES_LABEL[s], color=SERIES_COLOR[s])
    ax.set_xticks(range(len(clients)))
    ax.set_xticklabels([str(c) for c in clients])
    ax.set_xlabel("Concurrent students")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    if log:
        ax.set_yscale("log")
    if percent:
        ax.set_ylim(0, 105)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def timeline(trials, clients, path):
    """Per-second mean answer RTT of the first repetition of each series."""
    fig, ax = plt.subplots(figsize=(9, 4.5))
    plotted = False
    marks = {}
    for s in SERIES_ORDER:
        match = [t for t in trials if t["record"]["series"] == s and t["record"]["clients"] == clients]
        if not match:
            continue
        t = sorted(match, key=lambda x: x["record"]["rep"])[0]
        t0 = t["meta"]["t_load"]
        buckets = defaultdict(list)
        for r in t["rows"]:
            if r["kind"] == "answer":
                buckets[int(r["t"] - t0)].append(r["value"])
        if not buckets:
            continue
        xs = sorted(buckets)
        ax.plot(xs, [statistics.mean(buckets[x]) for x in xs], label=SERIES_LABEL[s], color=SERIES_COLOR[s], linewidth=1.8)
        plotted = True
        if t["meta"]["flood_start"]:
            marks["flood"] = t["meta"]["flood_start"] - t0
            mit = next((e for e in t["events"] if e["event"] == "MITIGATION_INSTALLED"), None)
            if mit:
                marks[f"{s} mitigation"] = (mit["ts"] - t0, SERIES_COLOR[s])
    if not plotted:
        plt.close(fig)
        return
    if "flood" in marks:
        ax.axvline(marks["flood"], color="black", linestyle="--", linewidth=1)
        ax.text(marks["flood"], ax.get_ylim()[1], " flood starts", va="top", fontsize=8)
    for key, val in marks.items():
        if key != "flood":
            ax.axvline(val[0], color=val[1], linestyle=":", linewidth=1.5)
            ax.text(val[0], ax.get_ylim()[1] * 0.8, " controller\n quarantines", va="top", fontsize=8, color=val[1])
    ax.set_yscale("log")
    ax.set_xlabel("Seconds since the load started")
    ax.set_ylabel("Mean answer response time (ms, log scale)")
    ax.set_title(f"Response time over time, {clients} students (first repetition)")
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", help="experiment folders (from run_experiments.py)")
    ap.add_argument("--out", default="results/analysis")
    args = ap.parse_args()

    trials = load_trials(args.folders)
    if not trials:
        sys.exit("No trials found (expected <folder>/<trial>/meta.json).")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "trials.csv", [t["record"] for t in trials])
    agg = aggregate(trials)
    write_csv(out / "aggregate.csv", agg)
    write_markdown(out / "summary.md", agg)
    bar_chart(agg, "rtt_mean_ms", "Mean answer response time (ms, log scale)", "Response time while the flood is active", out / "rtt_mean.png", log=True)
    bar_chart(agg, "rtt_p95_ms", "95th percentile response time (ms, log scale)", "Tail latency while the flood is active", out / "rtt_p95.png", log=True)
    bar_chart(agg, "exams_per_s", "Completed exams per second", "Throughput", out / "throughput.png")
    bar_chart(agg, "success_rate", "Exams completed (%)", "Success rate", out / "success.png", percent=True)
    for c in sorted({t["record"]["clients"] for t in trials}):
        timeline(trials, c, out / f"timeline_n{c}.png")
    print((out / "summary.md").read_text())
    print(f"Charts and tables written to {out}")


if __name__ == "__main__":
    main()
