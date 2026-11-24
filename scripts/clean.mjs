import { rm } from 'node:fs/promises';

const root = new URL('../', import.meta.url);
for (const path of ['dist/', 'typescript/dist/', 'python/dist/', 'python/build/', '.ruff_cache/']) {
  await rm(new URL(path, root), { recursive: true, force: true });
}
