const { invoke } = (window as any).__TAURI__.core;

const apiKeyInput = document.getElementById("api-key") as HTMLInputElement;
const saveBtn = document.getElementById("save-btn") as HTMLButtonElement;
const status = document.getElementById("status") as HTMLDivElement;

async function loadKey() {
  try {
    const key = await invoke("get_api_key");
    if (key) apiKeyInput.value = key;
  } catch (e) {
    console.error("Failed to load key", e);
  }
}

saveBtn.addEventListener("click", async () => {
  const key = apiKeyInput.value.trim();
  if (!key) {
    status.textContent = "Key cannot be empty";
    status.style.color = "#f44747";
    return;
  }
  try {
    await invoke("set_api_key", { key });
    status.textContent = "Saved!";
    status.style.color = "#6a9955";
  } catch (e: any) {
    status.textContent = `Error: ${e}`;
    status.style.color = "#f44747";
  }
});

loadKey();
