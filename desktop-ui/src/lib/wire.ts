// Browser half of the end-to-end encrypted channel. Must stay byte-compatible
// with src/agent8s/desktop/remote_crypto.py (the docstring there is the spec);
// tests/test_wire_interop.py checks both directions against real Node/WebCrypto.
// No DOM access on purpose, so it runs under Node for that test.

const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();

export const C2H = 'c2h';
export const H2C = 'h2c';
const COMPRESS_OVER = 1024;

export interface Keys {
  room: string;
  enc: CryptoKey;
}

export function b64url(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function b64urlDecode(text: string): Uint8Array<ArrayBuffer> {
  const bin = atob(text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

const toHex = (bytes: Uint8Array) => [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');

export const randomHex = (bytes: number) => toHex(crypto.getRandomValues(new Uint8Array(bytes)));

/** room id + a non-extractable AES key from the 32-byte pairing secret. */
export async function deriveKeys(secret: Uint8Array<ArrayBuffer>): Promise<Keys> {
  if (secret.length !== 32) throw new Error('Неверная длина ключа');
  const base = await crypto.subtle.importKey('raw', secret, 'HKDF', false, ['deriveBits', 'deriveKey']);
  const params = (info: string) => ({
    name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: textEncoder.encode(info),
  });
  const room = await crypto.subtle.deriveBits(params('agent8s/room/v1'), base, 128);
  const enc = await crypto.subtle.deriveKey(
    params('agent8s/enc/v1'), base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt'],
  );
  return { room: toHex(new Uint8Array(room)), enc };
}

async function through(data: Uint8Array<ArrayBuffer>, stream: CompressionStream | DecompressionStream) {
  const piped = new Blob([data]).stream().pipeThrough(stream as unknown as ReadableWritablePair<Uint8Array, Uint8Array>);
  return new Uint8Array(await new Response(piped).arrayBuffer());
}

const aad = (keys: Keys, direction: string) => textEncoder.encode(`agent8s/v1|${direction}|${keys.room}`);

export async function seal(keys: Keys, direction: string, obj: unknown): Promise<string> {
  let body = textEncoder.encode(JSON.stringify(obj)) as Uint8Array<ArrayBuffer>;
  let flag = 0;
  if (body.length > COMPRESS_OVER) {
    const packed = await through(body, new CompressionStream('deflate'));
    if (packed.length < body.length) {
      body = packed as Uint8Array<ArrayBuffer>;
      flag = 1;
    }
  }
  const plain = new Uint8Array(body.length + 1);
  plain[0] = flag;
  plain.set(body, 1);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(
    await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: aad(keys, direction) }, keys.enc, plain),
  );
  const out = new Uint8Array(12 + ct.length);
  out.set(iv);
  out.set(ct, 12);
  return b64url(out);
}

/** Decrypt a frame; null for anything forged, corrupt or for another room. */
export async function open(keys: Keys, direction: string, frame: string): Promise<any | null> {
  try {
    const blob = b64urlDecode(frame);
    if (blob.length < 29) return null;
    const plain = new Uint8Array(
      await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: blob.slice(0, 12), additionalData: aad(keys, direction) }, keys.enc, blob.slice(12),
      ),
    );
    let body = plain.slice(1) as Uint8Array<ArrayBuffer>;
    if (plain[0] === 1) body = (await through(body, new DecompressionStream('deflate'))) as Uint8Array<ArrayBuffer>;
    else if (plain[0] !== 0) return null;
    return JSON.parse(textDecoder.decode(body));
  } catch {
    return null;
  }
}
