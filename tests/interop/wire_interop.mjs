// Run by tests/test_wire_interop.py. Reads {frames, secret} as JSON on stdin; verifies
// frames sealed by Python, and prints frames sealed here for Python to verify.
import { C2H, H2C, b64urlDecode, deriveKeys, open, seal } from '../../desktop-ui/src/lib/wire.ts';

const input = JSON.parse(await new Promise((resolve) => {
  let data = '';
  process.stdin.on('data', (c) => (data += c)).on('end', () => resolve(data));
}));

const keys = await deriveKeys(b64urlDecode(input.secret));
const opened = {};
for (const [name, frame] of Object.entries(input.frames)) opened[name] = await open(keys, H2C, frame);

const small = { k: 'req', id: 1, p: '/api/bootstrap', note: 'привет' };
const big = { k: 'res', b: { text: 'длинный текст '.repeat(4000) } };
console.log(JSON.stringify({
  room: keys.room,
  opened,
  sealed: {
    small: await seal(keys, C2H, small),
    big: await seal(keys, C2H, big),
  },
  // a frame addressed the wrong way must not open
  reflected: await open(keys, C2H, input.frames.small),
}));
