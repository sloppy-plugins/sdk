import { createLogger, defineConfig } from 'vite';
import dts from 'vite-plugin-dts';

const logger = createLogger();
logger.warn = logger.warnOnce = (message) => {
  throw new Error(message);
};

export default defineConfig({
  customLogger: logger,
  plugins: [dts({ include: ['src/**/*.ts'] })],
  build: {
    target: 'node22',
    lib: { entry: 'src/index.ts', formats: ['es'], fileName: 'index' },
    rolldownOptions: {
      external: ['ws', '@peculiar/x509', 'reflect-metadata', /^node:/],
      onwarn(warning) {
        throw new Error(warning.message);
      },
    },
    sourcemap: true,
  },
});
