import { useMemo } from "react";
import type { BranchNode } from "./api";
import { shortId } from "./eventStyle";

interface Props {
  branches: BranchNode[];
  selectedBranch: string;
  onSelect: (branchId: string) => void;
  onCheckpointClick?: (branchId: string) => void;
}

const LANE_H = 68;
const LEFT_PAD = 170;
const RIGHT_PAD = 60;
const TOP_PAD = 30;
// Main is the mainline — always the brightest rail, pure white. Every
// fork after it draws its own saturated hue from the signal set, so the
// board reads by colour the way a real switch yard does: no two branches
// that could be confused for one another.
const COLORS = ["#ffffff", "#ff8a3d", "#2fe6d1", "#ffd23f", "#9b7bff", "#ff5fa8"];

export default function DagGraph({ branches, selectedBranch, onSelect, onCheckpointClick }: Props) {
  const { lanes, maxSeq, colorOf } = useMemo(() => {
    const order = [...branches].sort((a, b) => (a.branch_id === "main" ? -1 : a.fork_seq_num - b.fork_seq_num));
    const laneIndex = new Map<string, number>();
    order.forEach((b, i) => laneIndex.set(b.branch_id, i));
    const colorMap = new Map<string, string>();
    order.forEach((b, i) => colorMap.set(b.branch_id, COLORS[i % COLORS.length]));
    const maxHead = Math.max(1, ...branches.map((b) => b.head_seq_num));
    return { lanes: order.map((b) => ({ branch: b, lane: laneIndex.get(b.branch_id)! })), maxSeq: maxHead, colorOf: colorMap };
  }, [branches]);

  const width = Math.max(900, LEFT_PAD + RIGHT_PAD + 900);
  const xScale = (seq: number) => LEFT_PAD + (seq / maxSeq) * (width - LEFT_PAD - RIGHT_PAD);
  const yOf = (branchId: string) => TOP_PAD + (lanes.find((l) => l.branch.branch_id === branchId)?.lane ?? 0) * LANE_H;
  const height = TOP_PAD * 2 + lanes.length * LANE_H;

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <defs>
        {/* filterUnits="userSpaceOnUse" with explicit, generous bounds —
            the default objectBoundingBox percentage region collapses to
            zero on a perfectly horizontal line (zero-height bbox), which
            silently clips the glow (and on some renderers the stroke
            itself) to nothing. That's why "main" looked unlit: its rail
            is the straightest, longest line in the graph — the case that
            trips the bug hardest. */}
        <filter id="glow" filterUnits="userSpaceOnUse" x={-30} y={-30} width={width + 60} height={height + 60}>
          <feGaussianBlur stdDeviation="3.2" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        <filter id="glow-soft" filterUnits="userSpaceOnUse" x={-30} y={-30} width={width + 60} height={height + 60}>
          <feGaussianBlur stdDeviation="1.6" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {lanes.map(({ branch, lane }) => {
        const y = TOP_PAD + lane * LANE_H;
        const color = colorOf.get(branch.branch_id)!;
        const x0 = branch.parent_branch_id ? xScale(branch.fork_seq_num) : LEFT_PAD - 40;
        const x1 = xScale(branch.head_seq_num);
        const selected = branch.branch_id === selectedBranch;

        return (
          <g key={branch.branch_id}>
            {/* fork connector from parent lane — the switch lead-in */}
            {branch.parent_branch_id && (
              <path
                d={`M ${xScale(branch.fork_seq_num)} ${yOf(branch.parent_branch_id)} C ${xScale(branch.fork_seq_num) + 30} ${yOf(branch.parent_branch_id)}, ${x0 - 30} ${y}, ${x0} ${y}`}
                stroke={color}
                strokeWidth={2}
                strokeOpacity={0.55}
                fill="none"
                filter="url(#glow-soft)"
              />
            )}

            {/* pool connectors */}
            {branch.pool_from_branch_ids?.map((poolId) => {
              const pooledBranch = branches.find((b) => b.branch_id === poolId);
              if (!pooledBranch) return null;
              const poolY = yOf(poolId);
              const poolX = xScale(pooledBranch.head_seq_num);
              return (
                <path
                  key={`pool-${poolId}`}
                  d={`M ${poolX} ${poolY} C ${poolX + 30} ${poolY}, ${x0 - 30} ${y}, ${x0} ${y}`}
                  stroke={colorOf.get(poolId) || color}
                  strokeWidth={1.2}
                  strokeOpacity={0.5}
                  strokeDasharray="4 3"
                  fill="none"
                />
              );
            })}

            {/* the rail — bright and lit; a paused/forked branch dims and
                dashes slightly but never goes dark, it's still live track */}
            <line
              x1={x0}
              y1={y}
              x2={x1}
              y2={y}
              stroke={color}
              strokeWidth={branch.branch_id === "main" ? (selected ? 4.5 : 3.5) : (selected ? 3.5 : 2.5)}
              strokeOpacity={branch.live ? 1 : 0.85}
              strokeDasharray={branch.live ? undefined : "5 4"}
              strokeLinecap="round"
              filter="url(#glow)"
            />

            {/* label */}
            <g
              onClick={() => onSelect(branch.branch_id)}
              style={{ cursor: "pointer" }}
            >
              <rect x={8} y={y - 13} width={LEFT_PAD - 26} height={26} rx={13}
                fill={selected ? color : "transparent"}
                fillOpacity={selected ? 0.14 : 1}
                stroke={selected ? color : "transparent"} strokeWidth={1.5} />
              {branch.live && (
                <circle cx={20} cy={y} r={3.5} fill={color} filter="url(#glow-soft)">
                  <animate attributeName="opacity" values="1;0.35;1" dur="1.6s" repeatCount="indefinite" />
                </circle>
              )}
              <text x={branch.live ? 30 : 18} y={y + 4} fontFamily="Space Grotesk, sans-serif" fontSize={branch.branch_id === "main" ? 13 : 11.5}
                fontWeight={branch.branch_id === "main" ? 700 : (selected ? 700 : 600)} fill={selected ? color : "var(--text)"}>
                {branch.branch_id === "main" ? "MAIN BRANCH" : branch.name}
              </text>
            </g>

            {/* head commit — the live end of the rail */}
            <circle
              cx={x1}
              cy={y}
              r={selected ? 7 : 5.5}
              fill={branch.live ? color : "var(--bg)"}
              stroke={color}
              strokeWidth={2}
              filter={branch.live ? "url(#glow)" : "url(#glow-soft)"}
              style={{ cursor: "pointer" }}
              onClick={() => onSelect(branch.branch_id)}
            />
            <text x={x1 + 11} y={y + 4} fontFamily="JetBrains Mono, monospace" fontSize={10} fill="var(--text-faint)">
              #{branch.head_seq_num}
            </text>

            {/* fork origin — two interlocking discs, parent's colour and
                this branch's, echoing the interlocking-circles mark in
                the topbar: a fork *is* one rail overlapping into another. */}
            {branch.parent_branch_id && (
              <InterlockGlyph
                x={xScale(branch.fork_seq_num)}
                y={y}
                parentY={yOf(branch.parent_branch_id)}
                colorA={colorOf.get(branch.parent_branch_id)!}
                colorB={color}
              />
            )}

            {/* checkpoint markers — waypoints this branch can be forked from */}
            {branch.checkpoint_seq_nums.map((seq) => (
              <g
                key={`cp-${branch.branch_id}-${seq}`}
                transform={`translate(${xScale(seq)}, ${y}) rotate(45)`}
                style={{ cursor: onCheckpointClick ? "pointer" : "default" }}
                onClick={() => {
                  onSelect(branch.branch_id);
                  onCheckpointClick?.(branch.branch_id);
                }}
              >
                <title>{`checkpoint @ #${seq}`}</title>
                <rect x={-4.5} y={-4.5} width={9} height={9} fill="var(--bg)" stroke={color} strokeWidth={2} filter="url(#glow-soft)" />
              </g>
            ))}
          </g>
        );
      })}
    </svg>
  );
}

/** Two overlapping discs at a fork point — parent's rail colour and the
 * new branch's, the same interlocking-circles shape as the topbar mark,
 * because a fork is exactly that: one rail's colour overlapping another. */
function InterlockGlyph({
  x, y, parentY, colorA, colorB,
}: { x: number; y: number; parentY: number; colorA: string; colorB: string }) {
  return (
    <g>
      {/* waypoint back on the parent's own lane, where the split happened */}
      <circle cx={x} cy={parentY} r={3.5} fill="var(--bg)" stroke={colorA} strokeWidth={2} filter="url(#glow-soft)" />
      <circle cx={x - 4} cy={y} r={6} fill={colorA} opacity={0.9} filter="url(#glow-soft)" />
      <circle cx={x + 4} cy={y} r={6} fill={colorB} opacity={0.75} filter="url(#glow-soft)" />
    </g>
  );
}
