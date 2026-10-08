/* Start or build the sample Mac app from the correct desktop folder.
   This wrapper passes command arguments to Tauri and returns its exit status so
   the desktop config and React asset paths resolve. It does not start a Python
   research host. */
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const desktopRoot = fileURLToPath(new URL('.', import.meta.url));
const repositoryRoot = resolve(desktopRoot, '..');
const projectRoot = resolve(desktopRoot, 'src-tauri');
const command = process.argv[2];

if (command !== 'dev' && command !== 'build') {
  throw new Error('Expected a Tauri dev or build command.');
}

const cli = resolve(repositoryRoot, 'node_modules/.bin/tauri');
const child = spawn(cli, [command, ...process.argv.slice(3)], {
  cwd: projectRoot,
  stdio: 'inherit',
  env: process.env,
});

child.on('error', error => {
  process.stderr.write(`Could not start Tauri: ${error.message}\n`);
  process.exitCode = 1;
});
child.on('exit', (code, signal) => {
  process.exitCode = code ?? (signal ? 1 : 0);
});
