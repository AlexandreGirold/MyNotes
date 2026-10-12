# Dependencies and licenses

Declared licenses from package manifests and documentation. **License files in installed packages have not been audited.** This document tracks metadata only — verification of actual license files in `.venv/` and `node_modules/` is pending per Plan.md Section 3 ("Pin versions and verify their license files when implementing").

## Python dependencies (uv / pyproject.toml)

| Package | Declared license | Upstream URL | Status |
|---------|-----------------|--------------|--------|
| fastapi | MIT | https://fastapi.tiangolo.com/ | Metadata only |
| uvicorn | MIT | https://www.uvicorn.org/ | Metadata only |
| python-multipart | Apache-2.0 | https://github.com/Starlit-King/python-multipart | Metadata only |
| httpx | BSD-3-Clause | https://www.python-httpx.org/ | Metadata only |
| cryptography | Apache-2.0 / BSD-3-Clause | https://cryptography.io/ | Metadata only |
| pypdfium2 | Apache-2.0 / BSD-style | https://pypdfium2.readthedocs.io/ | Metadata only |
| python-pptx | MIT | https://python-pptx.readthedocs.io/ | Metadata only |
| beautifulsoup4 | MIT | https://www.crummy.com/software/BeautifulSoup/ | Metadata only |
| Pillow | HPND (Historical Permission Notice and Disclaimer) | https://python-pillow.org/ | Metadata only |
| mcp (MCP Python SDK) | MIT | https://github.com/modelcontextprotocol/python-sdk | Metadata only |
| sherpa-onnx / sherpa-onnx-core | Apache-2.0 | https://github.com/k2-fsa/sherpa-onnx | CPU runtime; versions pinned together |
| faster-whisper | MIT | https://github.com/SYSTRAN/faster-whisper | CPU INT8 timestamp anchors |
| CTranslate2 | MIT | https://github.com/OpenNMT/CTranslate2 | Inference runtime dependency |

### Optional downloaded audio models

- Pyannote segmentation 3.0 ONNX: model upstream https://huggingface.co/pyannote/segmentation-3.0 (MIT); converted distribution includes its license.
- TitaNet Small ONNX: exported NeMo model distributed by Sherpa-ONNX; retain upstream model notices when redistributing weights.
- Systran/faster-whisper-tiny: https://huggingface.co/Systran/faster-whisper-tiny (MIT).

Weights are downloaded explicitly by the user, not included in the desktop package.

### Dev dependencies

| Package | Declared license | Upstream URL | Status |
|---------|-----------------|--------------|--------|
| ruff | MIT | https://docs.astral.sh/ruff/ | Metadata only |
| pyinstaller | GPL with distribution exception | https://www.pyinstaller.org/ | Metadata only |
| pytest | MIT | https://docs.pytest.org/ | Metadata only |

> **Note:** PyInstaller is distributed under GPL with a distribution exception. Verify the exception text in the installed license file before redistributing a PyInstaller-bundled app.

## Node.js dependencies (package.json)

| Package | Declared license | Upstream URL | Status |
|---------|-----------------|--------------|--------|
| react | MIT | https://react.dev/ | Metadata only |
| react-dom | MIT | https://react.dev/ | Metadata only |
| pdfjs-dist | Apache-2.0 | https://mozilla.github.io/pdf.js/ | Metadata only |
| @marp-team/marp-cli | MIT | https://marp.app/ | Metadata only |
| @testing-library/jest-dom | MIT | https://testing-library.com/docs/react-testing-library/api/ | Metadata only |
| @testing-library/react | MIT | https://testing-library.com/docs/react-testing-library/ | Metadata only |
| @types/react, @types/react-dom | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped | Metadata only |
| @vitejs/plugin-react | MIT | https://github.com/vitejs/vite-plugin-react | Metadata only |
| jsdom | MIT | https://jsdom.github.io/ | Metadata only |
| typescript | Apache-2.0 | https://www.typescriptlang.org/ | Metadata only |
| vite | MIT | https://vitejs.dev/ | Metadata only |
| vitest | MIT | https://vitest.dev/ | Metadata only |

## Electron

| Package | Declared license | Upstream URL | Status |
|---------|-----------------|--------------|--------|
| Electron | MIT | https://www.electronjs.org/ | Metadata only |

Electron is the application delivery shell (Chromium + Node.js). It is not listed as a dependency in `package.json` but is a core component per the architecture plan.

## Notes

- All licenses above are declared from project manifests (`pyproject.toml`, `package.json`) and publicly documented tool descriptions. **No license files in `.venv/` or `node_modules/` have been audited.**
- The Plan.md states: "Pin versions and verify their license files when implementing." This verification is pending.
- To audit, inspect `LICENSE`, `LICENSE.md`, or `COPYING` files in each package directory under `.venv/lib/` (Python) and `node_modules/` (Node.js).
- Any license that conflicts with redistribution (e.g., a copyleft license without an appropriate exception) should be reviewed before creating a distributable installer.
