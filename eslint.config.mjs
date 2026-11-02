/* Check the new live-data boundary strictly without hiding older hook debt.
   Babel parses TypeScript syntax; tsc remains the type authority. */
import parser from '@babel/eslint-parser';
import hooks from 'eslint-plugin-react-hooks';
export default [{
  files: ['ui/src/live*.ts', 'ui/src/LiveResearchApp.tsx', 'ui/src/useLiveUpdates.ts'],
  ignores: ['**/*.test.ts', '**/*.test.tsx'],
  languageOptions: { parser, parserOptions: { requireConfigFile: false, babelOptions: { parserOpts: { plugins: ['typescript', 'jsx'] } } } },
  plugins: { 'react-hooks': hooks },
  rules: { 'react-hooks/rules-of-hooks': 'error', 'react-hooks/exhaustive-deps': 'error' },
}];
