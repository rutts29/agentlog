import type { TreeNode } from "@/lib/api";

export const CLIENT_TREE_MAX_NODES = 500;
export const CLIENT_TREE_MAX_DEPTH = 64;

export type BranchTreeRow = {
  node: TreeNode;
  depth: number;
  parentIndex: number | null;
};

export type BranchTreeProjection = {
  rows: BranchTreeRow[];
  omittedNodeCount: number;
  truncated: boolean;
};

export function isWorkerTreeNode(node: Pick<TreeNode, "thread_source" | "relationship">): boolean {
  return (
    node.relationship === "provider_worker" ||
    node.thread_source === "subagent" ||
    node.thread_source === "workflow_subagent"
  );
}

export function sessionTreeLabel(
  node: Pick<TreeNode, "thread_source" | "relationship">,
  depth: number,
): "Agent run" | "Main" | "Worker" | "Branch" {
  if (depth === 0) {
    return node.thread_source === "autonomous_agent_unlinked" ? "Agent run" : "Main";
  }
  return isWorkerTreeNode(node) ? "Worker" : "Branch";
}

function nonnegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

function descendantHint(node: TreeNode): number {
  return Math.max(
    nonnegativeInteger(node.descendant_count) ?? 0,
    nonnegativeInteger(node.omitted_descendant_count) ?? 0,
    Array.isArray(node.children) ? node.children.length : 0,
  );
}

export function projectBranchTree(
  root: TreeNode | undefined,
  maxNodes = CLIENT_TREE_MAX_NODES,
  maxDepth = CLIENT_TREE_MAX_DEPTH,
): BranchTreeProjection {
  if (!root || maxNodes < 1 || maxDepth < 0) {
    return { rows: [], omittedNodeCount: root ? 1 : 0, truncated: Boolean(root) };
  }

  const rows: BranchTreeRow[] = [];
  const pending: Array<{
    node: TreeNode;
    depth: number;
    parentIndex: number | null;
  }> = [{ node: root, depth: 0, parentIndex: null }];
  let omittedNodeCount = 0;

  while (pending.length > 0 && rows.length < maxNodes) {
    const current = pending.pop()!;
    const rowIndex = rows.length;
    rows.push(current);

    const children = Array.isArray(current.node.children)
      ? current.node.children
      : [];
    if (current.depth >= maxDepth) {
      omittedNodeCount += descendantHint(current.node);
      continue;
    }
    for (let index = children.length - 1; index >= 0; index -= 1) {
      pending.push({
        node: children[index],
        depth: current.depth + 1,
        parentIndex: rowIndex,
      });
    }
  }

  omittedNodeCount = Math.max(omittedNodeCount, pending.length);
  const declaredTotal = nonnegativeInteger(root.descendant_count);
  if (declaredTotal !== null) {
    omittedNodeCount = Math.max(omittedNodeCount, declaredTotal + 1 - rows.length);
  }
  omittedNodeCount = Math.max(
    0,
    omittedNodeCount,
    nonnegativeInteger(root.omitted_descendant_count) ?? 0,
  );

  return {
    rows,
    omittedNodeCount,
    truncated: omittedNodeCount > 0,
  };
}

export function findBranchPath(
  rows: BranchTreeRow[],
  ids: ReadonlySet<string>,
): TreeNode[] | null {
  let index = rows.findIndex(
    ({ node }) => ids.has(node.id) || Boolean(node.navigation_id && ids.has(node.navigation_id)),
  );
  if (index < 0) return null;

  const path: TreeNode[] = [];
  while (index >= 0) {
    const row = rows[index];
    path.push(row.node);
    index = row.parentIndex ?? -1;
  }
  path.reverse();
  return path;
}
