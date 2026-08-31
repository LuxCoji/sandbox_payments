import { useMemo } from "react";
import type { BranchNode } from "./api";
import { shortId } from "./eventStyle";

interface Props {
  branches: BranchNode[];
  selectedBranch: string;
  onSelect: (branchId: string) => void;
  onCheckpointClick?: (branchId: string) => void;
}

const LANE_H = 64;
const LEFT_PAD = 170;
const RIGHT_PAD = 60;
const TOP_PAD = 30;
const COLORS = ["#5ef2b5", "#6fb7ff", "#b78bff", "#ffb454", "#ff9ecb", "#8892a3"];

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
        <filter id="glow" x="-100%" y="-100%" width="300%" height="300%">
          <feGaussianBlur stdDeviation="3" result="blur" />
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
            {/* fork connector from parent lane */}
            {branch.parent_branch_id && (
              <path
                d={`M ${xScale(branch.fork_seq_num)} ${yOf(branch.parent_branch_id)} C ${xScale(branch.fork_seq_num) + 30} ${yOf(branch.parent_branch_id)}, ${x0 - 30} ${y}, ${x0} ${y}`}
                stroke={color}
                strokeWidth={1.5}
                strokeOpacity={0.45}
                fill="none"
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
                  strokeWidth={1}
                  strokeOpacity={0.4}
                  strokeDasharray="4 2"
                  fill="none"
                />
              );
            })}

            {/* lane line glow (prevents SVG zero-height bounding box clip bug) */}
            {branch.branch_id === "main" && (
              <line
                x1={x0}
                y1={y}
                x2={x1}
                y2={y}
                stroke={color}
                strokeWidth={12}
                strokeOpacity={0.15}
              />
            )}

            {/* lane line */}
            <line
              x1={x0}
              y1={y}
              x2={x1}
              y2={y}
              stroke={color}
              strokeWidth={branch.branch_id === "main" ? (selected ? 4 : 3) : (selected ? 3 : 2)}
              strokeOpacity={branch.live ? 1 : 0.6}
              strokeDasharray={branch.live ? undefined : "3 4"}
            />

            {/* label */}
            <g
              onClick={() => onSelect(branch.branch_id)}
              style={{ cursor: "pointer" }}
            >
              <rect x={8} y={y - 12} width={LEFT_PAD - 26} height={24} rx={6}
                fill={selected ? "rgba(94,242,181,0.1)" : "transparent"}
                stroke={selected ? color : "transparent"} strokeWidth={1} />
              {branch.live && (
                <circle cx={20} cy={y} r={3.5} fill={color}>
                  <animate attributeName="opacity" values="1;0.3;1" dur="1.6s" repeatCount="indefinite" />
                </circle>
              )}
              <text x={branch.live ? 30 : 18} y={y + 4} fontFamily="JetBrains Mono, monospace" fontSize={branch.branch_id === "main" ? 13 : 11.5}
                fontWeight={branch.branch_id === "main" ? 800 : (selected ? 700 : 500)} fill={selected ? color : "#c7cedb"}>
                {branch.branch_id === "main" ? "MAIN BRANCH" : branch.name}
              </text>
            </g>

            {/* head commit dot */}
            <circle
              cx={x1}
              cy={y}
              r={selected ? 6 : 5}
              fill={branch.live ? color : "#0d1119"}
              stroke={color}
              strokeWidth={2}
              filter={branch.live ? "url(#glow)" : undefined}
              style={{ cursor: "pointer" }}
              onClick={() => onSelect(branch.branch_id)}
            />
            <text x={x1 + 10} y={y + 4} fontFamily="JetBrains Mono, monospace" fontSize={10} fill="#4d5567">
              #{branch.head_seq_num}
            </text>

            {/* fork origin dot on parent lane */}
            {branch.parent_branch_id && (
              <circle cx={xScale(branch.fork_seq_num)} cy={yOf(branch.parent_branch_id)} r={3.5}
                fill="#06080c" stroke={colorOf.get(branch.parent_branch_id)} strokeWidth={2} />
            )}

            {/* checkpoint markers — snapshots this branch can be forked from */}
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
                <rect x={-4} y={-4} width={8} height={8} fill="#06080c" stroke={color} strokeWidth={1.5} />
              </g>
            ))}
          </g>
        );
      })}
    </svg>
  );
}
