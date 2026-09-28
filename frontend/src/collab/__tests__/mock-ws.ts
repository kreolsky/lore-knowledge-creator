/** Shared MockWebSocket for collab connection tests. */
// ARCH: Merged from near-identical copies in project-connection.test.ts and
// project-collab-connection.test.ts. The `sent` array is always available;
// callers that don't need it simply ignore it.

export class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];
  readyState = 0;
  url = '';
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string) { this.sent.push(data); }
  close() { this.readyState = 3; }

  simulateOpen() {
    this.readyState = 1;
    this.onopen?.();
  }
  simulateMessage(msg: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  simulateClose(code = 1000, reason = '') {
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }

  static reset() { MockWebSocket.instances = []; }
  static latest() { return MockWebSocket.instances[MockWebSocket.instances.length - 1]; }
}
