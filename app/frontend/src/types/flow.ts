// React Flow viewport (mirrors @xyflow/react's Viewport without coupling persistence to the lib).
export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

// Per-node data bag. `internal_state` is the saved configuration restored on load; the index
// signature keeps the persisted shape open (React Flow attaches other fields) and lets loosely
// shaped JSON from the backend assign cleanly.
export interface FlowNodeData {
  internal_state?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface FlowNode {
  id: string;
  type?: string;
  position?: { x: number; y: number };
  data?: FlowNodeData;
}

export interface FlowEdge {
  id: string;
  source: string;
  target: string;
  type?: string;
  data?: Record<string, unknown>;
}

export interface Flow {
  id: number;
  name: string;
  description?: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
  viewport?: Viewport;
  data?: Record<string, unknown>;
  is_template: boolean;
  tags?: string[];
  created_at: string;
  updated_at?: string;
}
