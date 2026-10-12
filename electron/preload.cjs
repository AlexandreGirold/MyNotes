'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('mynotes', {
  connection: () => ipcRenderer.invoke('connection'),
  getMicrophoneVolume: () => ipcRenderer.invoke('getMicrophoneVolume'),
  setMicrophoneVolume: volume => ipcRenderer.invoke('setMicrophoneVolume', volume),
  selectImport: () => ipcRenderer.invoke('selectImport'),
  selectDirectories: () => ipcRenderer.invoke('selectDirectories'),
  saveTextFile: request => ipcRenderer.invoke('saveTextFile', request),
  saveRecordingChunk: (id, index, data) => ipcRenderer.invoke('saveRecordingChunk', id, index, data),
  finishRecording: id => ipcRenderer.invoke('finishRecording', id),
});
