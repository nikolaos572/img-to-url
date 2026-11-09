// Upload page behaviour: clipboard paste, file picker, drag & drop, copy
// button. Configuration arrives via data-* attributes on this script tag
// because the site's Content-Security-Policy forbids inline scripts.

const configEl = document.getElementById('upload-config');
const CONFIG = {
  maxBytes: parseInt(configEl.getAttribute('data-max-bytes'), 10),
  maxMb: configEl.getAttribute('data-max-mb'),
  allowed: configEl.getAttribute('data-allowed').split(','),
};

// Extension to upload under when a file's own name has none we accept:
// images dragged in from another web page, Windows ".jfif" JPEGs, etc.
const MIME_EXTENSIONS = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/bmp': 'bmp',
  'image/avif': 'avif',
  'image/x-icon': 'ico',
  'image/vnd.microsoft.icon': 'ico',
};

// Show the platform's shortcut modifier: ⌘ on Apple devices, Ctrl elsewhere.
const platform = (navigator.userAgentData && navigator.userAgentData.platform) ||
                 navigator.platform || '';
const MOD_KEY = /mac|iphone|ipad|ipod/i.test(platform) ? '⌘' : 'Ctrl';
document.querySelectorAll('.mod-key').forEach((el) => { el.textContent = MOD_KEY; });

const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');
const status = document.getElementById('status');
const statusText = document.getElementById('status-text');
const errorBox = document.getElementById('error');
const resultBox = document.getElementById('result');
const resultLink = document.getElementById('result-link');
const resultPreview = document.getElementById('result-preview');
const resultUrl = document.getElementById('result-url');
const resultMeta = document.getElementById('result-meta');
const copyBtn = document.getElementById('copy-btn');

let latestUpload = 0;   // id of the newest upload; older ones must not touch the page
let previewUrl = null;  // object URL behind the preview, revoked when replaced
let copyResetTimer = null;

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove('hidden');
}

function setBusy(busy, text) {
  if (busy) {
    statusText.textContent = text || 'Uploading…';
    status.classList.remove('hidden');
  } else {
    status.classList.add('hidden');
  }
}

// The name to upload a file under: its own when the extension is accepted,
// else "image.<ext>" for an accepted MIME type, else null (unsupported).
function uploadName(file) {
  const dot = file.name.lastIndexOf('.');
  const ext = dot >= 0 ? file.name.slice(dot + 1).toLowerCase() : '';
  if (CONFIG.allowed.includes(ext)) return file.name;
  const fromType = MIME_EXTENSIONS[file.type];
  return fromType && CONFIG.allowed.includes(fromType) ? 'image.' + fromType : null;
}

function validateFile(file, name) {
  if (!name) {
    return 'This file type is not supported. Allowed types: ' + CONFIG.allowed.join(', ') + '.';
  }
  if (file.size > CONFIG.maxBytes) {
    return 'File is too large (' + humanSize(file.size) + '). ' +
           'The maximum is ' + CONFIG.maxMb + ' MB.';
  }
  return null;
}

function errorMessage(statusCode, data) {
  if (data && typeof data.detail === 'string') return data.detail;
  if (statusCode === 413) {
    // Refused before it reached the app, typically by a reverse proxy.
    return 'The server refused the file as too large (HTTP 413). If a reverse ' +
           'proxy runs in front of this site, raise its upload size limit.';
  }
  return 'Upload failed (' + statusCode + ').';
}

async function uploadFile(file) {
  // Uploads can overlap (e.g. two quick pastes). Only the newest one may
  // update the page, or a slower older one could replace its URL.
  const uploadId = ++latestUpload;
  errorBox.classList.add('hidden');
  resultBox.classList.add('hidden');

  const name = uploadName(file);
  const problem = validateFile(file, name);
  if (problem) {
    setBusy(false);
    showError(problem);
    return;
  }

  setBusy(true, 'Uploading ' + (file.name || name) + '…');
  try {
    const body = new FormData();
    body.append('file', file, name);
    const response = await fetch('/upload', { method: 'POST', body });

    if (response.status === 401) {
      window.location.assign('/login');
      return;
    }

    const data = await response.json().catch(() => ({}));
    if (uploadId !== latestUpload) return;  // superseded by a newer upload
    if (!response.ok) {
      showError(errorMessage(response.status, data));
      return;
    }

    showResult(data, file);
  } catch (err) {
    if (uploadId === latestUpload) {
      showError('Upload failed: ' + (err && err.message ? err.message : 'network error'));
    }
  } finally {
    if (uploadId === latestUpload) setBusy(false);
  }
}

function showResult(data, file) {
  resultUrl.value = data.url;
  resultMeta.textContent = 'Size: ' + humanSize(data.size) + ' · saved as ' + data.filename;
  // Preview straight from the local file: instant, and no second download.
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  resultPreview.src = previewUrl;
  resultLink.href = data.url;
  resultLink.classList.remove('hidden');
  resultBox.classList.remove('hidden');
  // Auto-select so Ctrl+C copies without touching the mouse.
  resultUrl.focus();
  resultUrl.select();
  resultUrl.setSelectionRange(0, data.url.length);
}

// A format this browser cannot display simply gets no thumbnail.
resultPreview.addEventListener('error', () => resultLink.classList.add('hidden'));

function humanSize(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit++;
  }
  return unit === 0 ? Math.round(size) + ' B' : size.toFixed(1) + ' ' + units[unit];
}

copyBtn.addEventListener('click', async () => {
  const url = resultUrl.value;
  let copied = false;
  try {
    await navigator.clipboard.writeText(url);
    copied = true;
  } catch (err) {
    resultUrl.focus();
    resultUrl.select();
    copied = document.execCommand('copy');
  }
  copyBtn.textContent = copied ? 'Copied!' : 'Press ' + MOD_KEY + '+C';
  copyBtn.classList.add('copied');
  clearTimeout(copyResetTimer);  // repeated clicks restart the countdown
  copyResetTimer = setTimeout(() => {
    copyBtn.textContent = 'Copy URL';
    copyBtn.classList.remove('copied');
  }, 1600);
});

fileInput.addEventListener('change', () => {
  const file = fileInput.files && fileInput.files[0];
  fileInput.value = '';  // allow re-picking the same file
  if (file) uploadFile(file);
});

// Pasting anywhere on the page uploads the clipboard image.
// Indexed loop, not for...of: DataTransferItemList is array-like but not
// iterable per spec, and for...of on it throws in some browsers.
document.addEventListener('paste', (event) => {
  const items = event.clipboardData && event.clipboardData.items;
  if (!items) return;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) {
        event.preventDefault();
        uploadFile(file);
        return;
      }
    }
  }
  // Nothing uploadable: say so rather than silently ignoring the paste.
  showError('The clipboard does not contain an image. Copy the image itself ' +
            '(not a link to it) and paste again.');
});

dropzone.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    fileInput.click();
  }
});

// Clicking anywhere in the dropzone opens the file picker. Clicks on the
// label button (or the input inside it) already do that natively.
dropzone.addEventListener('click', (event) => {
  if (!event.target.closest('label')) fileInput.click();
});

// Drag & drop. A file dropped anywhere on the page is uploaded, so a near
// miss of the dropzone doesn't make the browser navigate away to open it.
function carriesFiles(event) {
  const types = event.dataTransfer && event.dataTransfer.types;
  return Boolean(types) && Array.prototype.indexOf.call(types, 'Files') !== -1;
}

document.addEventListener('dragover', (event) => {
  if (carriesFiles(event)) event.preventDefault();
});
document.addEventListener('drop', (event) => {
  if (!carriesFiles(event)) return;
  event.preventDefault();
  dropzone.classList.remove('dragging');
  const file = event.dataTransfer.files[0];
  if (file) uploadFile(file);
});

['dragenter', 'dragover'].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    if (carriesFiles(event)) dropzone.classList.add('dragging');
  });
});
// dragleave also fires when the pointer moves onto the dropzone's own
// children; only un-highlight once it has really left the zone.
dropzone.addEventListener('dragleave', (event) => {
  if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove('dragging');
});
