/** Registers every installed module's frontend bundle -- the browser-side
 * counterpart to `server/core/modules.py`'s `install_enabled_modules()`.
 * That loader's own docstring states the rule this file exists to keep:
 * "core must never mention a fund... never a literal `import modules.funds`
 * anywhere." `import.meta.glob` resolves every `modules/<name>/index.ts`
 * at build time with no module name spelled out here, so adding a module's
 * frontend registration is one new `web/src/modules/<name>/` directory, not
 * an edit to this file or to `main.tsx`.
 */
import.meta.glob('./*/index.ts', { eager: true })
