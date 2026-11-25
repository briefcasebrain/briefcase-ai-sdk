/**
 * The bounded export queue between instrumented calls and the exporter.
 *
 * Records are queued in memory (default 10 000; when full, new records are
 * dropped and counted) and sent in batches. Console, file and memory
 * exporters drain on the next turn of the event loop; HTTP and OTLP drain
 * when a batch fills or every `flushIntervalMs`. Every timer is `unref()`ed,
 * so the queue never keeps a process alive. Records still queued when the
 * event loop empties are sent from a `beforeExit` hook; SIGTERM / SIGINT
 * flush (for up to 2 s) before the default signal action runs; `exit` writes
 * what it still can synchronously.
 */

import type { Exporter, ExportResult } from "./exporters.js";
import type { DecisionRecord } from "./record.js";
import { debug, describeError, warn } from "./util.js";

export interface PipelineStats {
  queued: number;
  exported: number;
  dropped: number;
  failed_batches: number;
  pending_calls: number;
}

/** A call whose record has not been emitted yet (an unread stream, an unparsed response). */
export interface PendingCall {
  abandon(): void;
}

export class Pipeline {
  readonly queue: DecisionRecord[] = [];
  exported = 0;
  dropped = 0;
  failedBatches = 0;
  private chain: Promise<void> = Promise.resolve();
  private immediate: ReturnType<typeof setImmediate> | null = null;
  private interval: ReturnType<typeof setInterval> | null = null;
  private warnedFull = false;
  closed = false;

  constructor(
    readonly exporter: Exporter,
    readonly maxQueue = 10_000,
  ) {}

  enqueue(record: DecisionRecord): void {
    if (this.closed) {
      this.dropped++;
      return;
    }
    if (this.queue.length >= this.maxQueue) {
      this.dropped++;
      if (!this.warnedFull) {
        this.warnedFull = true;
        warn(`export queue full (${this.maxQueue} records); dropping new records. See exportStats().dropped.`);
      }
      return;
    }
    this.warnedFull = false;
    if (this.exporter.immediate && this.exporter.exportSync) {
      try {
        this.exporter.exportSync([record]);
        this.exported++;
        return;
      } catch (error) {
        debug(`immediate export failed, queued instead: ${describeError(error)}`);
      }
    }
    this.queue.push(record);
    this.schedule();
  }

  private schedule(): void {
    const { batchSize, flushIntervalMs } = this.exporter;
    if (flushIntervalMs === 0 || this.queue.length >= batchSize) {
      if (this.immediate === null) {
        this.immediate = setImmediate(() => {
          this.immediate = null;
          void this.flush(false);
        });
        this.immediate.unref();
      }
    }
    if (flushIntervalMs > 0 && this.interval === null) {
      this.interval = setInterval(() => {
        if (this.queue.length > 0) void this.flush(false);
      }, flushIntervalMs);
      this.interval.unref();
    }
  }

  /** Sends everything queued. `final` skips backoff waits (shutdown / exit). */
  flush(final = false): Promise<void> {
    this.chain = this.chain.then(() => this.drain(final)).catch((error) => {
      debug(`drain failed: ${describeError(error)}`);
    });
    return this.chain;
  }

  private async drain(final: boolean): Promise<void> {
    const size = Math.max(1, this.exporter.batchSize === 1 ? Number.MAX_SAFE_INTEGER : this.exporter.batchSize);
    while (this.queue.length > 0) {
      const batch = this.queue.splice(0, size);
      let result: ExportResult;
      try {
        result = await this.exporter.exportBatch(batch, final);
      } catch (error) {
        warn(`${this.exporter.kind} exporter failed: ${describeError(error)}`);
        result = "retry";
      }
      if (result === "sent") {
        this.exported += batch.length;
        continue;
      }
      this.failedBatches++;
      if (result === "rejected") {
        this.dropped += batch.length;
        continue;
      }
      // Transient failure: keep the batch (oldest dropped past the bound) for the next flush.
      this.queue.unshift(...batch);
      const overflow = this.queue.length - this.maxQueue;
      if (overflow > 0) {
        this.queue.splice(0, overflow);
        this.dropped += overflow;
      }
      return;
    }
  }

  /** Synchronous last-chance export from the `exit` hook. */
  drainSync(): void {
    if (this.queue.length === 0) return;
    if (this.exporter.exportSync) {
      const batch = this.queue.splice(0, this.queue.length);
      try {
        this.exporter.exportSync(batch);
        this.exported += batch.length;
      } catch (error) {
        this.dropped += batch.length;
        debug(`exit export failed: ${describeError(error)}`);
      }
      return;
    }
    const lost = this.queue.length;
    this.queue.length = 0;
    this.dropped += lost;
    warn(
      `${lost} record(s) for ${this.exporter.describe()} were not sent: the process exited ` +
        "(process.exit) before they could be. Call `await shutdown()` before exiting.",
    );
  }

  stopTimers(): void {
    if (this.immediate !== null) clearImmediate(this.immediate);
    if (this.interval !== null) clearInterval(this.interval);
    this.immediate = null;
    this.interval = null;
  }

  async close(): Promise<void> {
    await this.flush(true);
    this.closed = true;
    this.stopTimers();
    try {
      await this.exporter.close?.();
    } catch (error) {
      debug(`exporter close failed: ${describeError(error)}`);
    }
  }
}
