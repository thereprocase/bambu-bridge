// Decode the bridge's Content-Length-framed MJPEG response across arbitrary
// HTTP chunk boundaries. Keep one connection; never poll or pace the source.
const MAX_FRAME = 8 * 1024 * 1024;
const MAX_HEADER = 4096;
const decoder = new TextDecoder();

export async function* jpegFrames(response) {
  if (!response.body || !response.headers.get('content-type')?.includes('multipart/x-mixed-replace')) {
    throw new Error('Camera stream unavailable.');
  }
  const reader = response.body.getReader();
  let buffer = new Uint8Array(0), length = null;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) throw new Error('Camera stream ended.');
      const next = new Uint8Array(buffer.length + value.length);
      next.set(buffer); next.set(value, buffer.length); buffer = next;
      while (true) {
        if (length === null) {
          let end = -1;
          for (let i = 0; i + 3 < Math.min(buffer.length, MAX_HEADER); i++) {
            if (buffer[i] === 13 && buffer[i+1] === 10 && buffer[i+2] === 13 && buffer[i+3] === 10) { end = i; break; }
          }
          if (end < 0) {
            if (buffer.length >= MAX_HEADER) throw new Error('Invalid camera frame header.');
            break;
          }
          const header = decoder.decode(buffer.subarray(0, end));
          const match = header.match(/(?:^|\r\n)Content-Length:\s*(\d+)\s*(?:\r\n|$)/i);
          length = match ? Number(match[1]) : 0;
          if (!header.trimStart().startsWith('--frame\r\n') || !Number.isSafeInteger(length) || length < 4 || length > MAX_FRAME) {
            throw new Error('Invalid camera frame length.');
          }
          buffer = buffer.subarray(end + 4);
        }
        if (buffer.length < length) break;
        const frame = buffer.slice(0, length);
        buffer = buffer.subarray(length); length = null;
        if (frame[0] !== 255 || frame[1] !== 216 || frame.at(-2) !== 255 || frame.at(-1) !== 217) {
          throw new Error('Invalid camera JPEG.');
        }
        yield frame;
      }
      if (buffer.length > MAX_FRAME) throw new Error('Camera frame too large.');
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
