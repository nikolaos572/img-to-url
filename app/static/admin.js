// Admin page behaviour, wired up here rather than through inline event
// handlers, which the site's Content-Security-Policy forbids.

// Confirmation dialogs for the destructive admin actions. The prompts
// live in data-confirm attributes.
document.querySelectorAll('form[data-confirm]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (!window.confirm(form.getAttribute('data-confirm'))) {
      event.preventDefault();
    }
  });
});

// Copy buttons: put a row's direct link (its data-copy value) on the clipboard.
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    // navigator.clipboard only exists on https (or localhost): fall back to
    // copying from a temporary, invisible text field.
    const previousFocus = document.activeElement;
    const field = document.createElement('textarea');
    field.value = text;
    field.readOnly = true;
    field.className = 'clipboard-helper';
    document.body.appendChild(field);
    field.select();
    field.setSelectionRange(0, text.length);
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch (execErr) {
      copied = false;
    }
    field.remove();
    if (previousFocus) previousFocus.focus();
    return copied;
  }
}

document.querySelectorAll('button[data-copy]').forEach((button) => {
  let resetTimer = null;
  button.addEventListener('click', async () => {
    const copied = await copyText(button.getAttribute('data-copy'));
    button.textContent = copied ? 'Copied!' : 'Copy failed';
    button.classList.toggle('copied', copied);
    clearTimeout(resetTimer);  // repeated clicks restart the countdown
    resetTimer = setTimeout(() => {
      button.textContent = 'Copy';
      button.classList.remove('copied');
    }, 1600);
  });
});
