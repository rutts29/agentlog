/* Minimal typings for force-graph's bundled physics engine. */
declare module "d3-force-3d" {
  export interface Force {
    (alpha: number): void;
    initialize?(nodes: unknown[]): void;
  }
  export type CollideForce = Force & {
    radius(r: number | ((node: any) => number)): CollideForce;
    iterations(n: number): CollideForce;
    strength(s: number): CollideForce;
  };
  export function forceCollide(
    radius?: number | ((node: any) => number),
  ): CollideForce;
  export function forceX(
    x?: number | ((node: any) => number),
  ): Force & { strength(s: number | ((node: any) => number)): Force };
  export function forceY(
    y?: number | ((node: any) => number),
  ): Force & { strength(s: number | ((node: any) => number)): Force };
}
