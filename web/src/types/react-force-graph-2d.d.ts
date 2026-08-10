/**
 * `d3AlphaTarget` is a real force-graph option (it forwards straight to
 * `simulation.alphaTarget`) that the shipped React typings omit. Declaring it
 * here keeps the spec's gentle-settle reheat type-safe.
 */
import type {} from "react-force-graph-2d";

declare module "react-force-graph-2d" {
  interface ForceGraphProps<NodeType = {}, LinkType = {}> {
    d3AlphaTarget?: number;
  }
}
