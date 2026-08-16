/**
 * Decision-record exporters: console stream, JSON Lines file, and in-memory.
 * All implement the TraceExporter port.
 */

import {
  closeSync,
  fchmodSync,
  fsyncSync,
  mkdirSync,
  openSync,
  writeSync,
} from "node:fs";
import { dirname } from "node:path";

import type { TraceExporter } from "./ports.js";

interface WritableLike {
  write(chunk: string): unknown;
}

/**
 * Writes each decision record as a line of JSON to a stream. Defaults to
 * process.stderr so it does not pollute program output.
 */
export class ConsoleExporter implements TraceExporter {
  private readonly stream: WritableLike;
  private readonly pretty: boolean;

  constructor(stream?: WritableLike, options: { pretty?: boolean } = {}) {
    this.stream = stream ?? process.stderr;
    this.pretty = options.pretty ?? false;
  }

  export(record: unknown): boolean {
    this.stream.write(JSON.stringify(record, null, this.pretty ? 2 : undefined) + "\n");
    return true;
  }

  flush(): void {}

  /** Never closes a shared stream like stderr. */
  close(): void {}
}

/**
 * Appends decision records to a file as JSON Lines. Parent directories are
 * created on demand with mode 0700 and the file is kept owner-only (0600):
 * records can carry decision content, so both stay private regardless of
 * umask. A pre-existing file is tightened to 0600 on first open. Export
 * after close reopens the file.
 */
export class JsonlFileExporter implements TraceExporter {
  private readonly path: string;
  private fd: number | null = null;

  constructor(path: string) {
    this.path = path;
    // mode is masked by umask, so created directories never exceed 0700.
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  }

  private ensureOpen(): number {
    if (this.fd === null) {
      this.fd = openSync(this.path, "a", 0o600);
      // fchmod on the open descriptor tightens a pre-existing file without
      // racing on the path.
      fchmodSync(this.fd, 0o600);
    }
    return this.fd;
  }

  export(record: unknown): boolean {
    const fd = this.ensureOpen();
    writeSync(fd, JSON.stringify(record) + "\n");
    return true;
  }

  flush(): void {
    if (this.fd !== null) fsyncSync(this.fd);
  }

  close(): void {
    if (this.fd !== null) {
      closeSync(this.fd);
      this.fd = null;
    }
  }
}

/** Collects decision records in memory for inspection in tests and notebooks. */
export class MemoryExporter implements TraceExporter {
  readonly records: unknown[] = [];

  export(record: unknown): boolean {
    this.records.push(record);
    return true;
  }

  flush(): void {}

  /** Keeps records so callers can still inspect them after close. */
  close(): void {}

  clear(): void {
    this.records.length = 0;
  }
}
