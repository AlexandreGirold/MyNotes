const { MakerZip } = require("@electron-forge/maker-zip");

/**
 * Minimal Forge config:
 *   - uses @electron-forge/maker-zip for Linux standalone zip (ASAR = app UI)
 *   - extraResources carry the PyInstaller onedir backend and tools
 */

module.exports = {
  packagerConfig: {
    name: "MyNotes",
    asar: true,
    download: { checksums: require("./node_modules/electron/checksums.json") },
    extraResource: ["resources/backend", "resources/tools"],
    ignore: [/^\/(\.venv|\.git|src|tests|resources|scripts|docs|node_modules|build|out)(\/|$)/],
  },
  makers: [
    {
      name: "@electron-forge/maker-zip",
      platforms: ["linux"],
      config: {
        archs: ["x64"],
      },
    },
  ],
};
