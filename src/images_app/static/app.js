document.addEventListener("error", (event) => {
  const image = event.target;
  if (!(image instanceof HTMLImageElement)) return;
  image.hidden = true;
  const fallback = image.parentElement?.querySelector(".image-unavailable");
  if (fallback) fallback.hidden = false;
}, true);

let draggedImageId = null;

document.addEventListener("dragstart", (event) => {
  const handle = event.target.closest?.(".drag-handle");
  if (!handle) return;
  const card = handle.closest(".image-card.is-selected");
  if (!card || !event.dataTransfer) return;
  draggedImageId = card.dataset.imageId;
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", draggedImageId);
  card.classList.add("is-dragging");
});

document.addEventListener("dragover", (event) => {
  const card = event.target.closest?.(".image-card.is-selected");
  if (!card || !draggedImageId || card.dataset.imageId === draggedImageId) return;
  event.preventDefault();
  card.classList.add("is-drop-target");
});

document.addEventListener("dragleave", (event) => {
  const card = event.target.closest?.(".image-card.is-selected");
  if (card && !card.contains(event.relatedTarget)) card.classList.remove("is-drop-target");
});

document.addEventListener("drop", async (event) => {
  const target = event.target.closest?.(".image-card.is-selected");
  if (!target || !draggedImageId || target.dataset.imageId === draggedImageId) return;
  event.preventDefault();
  const gallery = target.closest("#gallery");
  const source = gallery?.querySelector(`.image-card[data-image-id="${CSS.escape(draggedImageId)}"]`);
  if (!gallery || !source) return;
  const bounds = target.getBoundingClientRect();
  const after = event.clientY > bounds.top + bounds.height / 2;
  target.parentElement.insertBefore(source, after ? target.nextSibling : target);
  gallery.querySelectorAll(".is-drop-target").forEach((card) => card.classList.remove("is-drop-target"));

  const form = gallery.querySelector("#reorder-votes-form");
  const order = [...gallery.querySelectorAll(".image-card.is-selected[data-image-id]")].map((card) => card.dataset.imageId);
  if (!form || order.length < 2) return;
  const body = new FormData(form);
  order.forEach((id) => body.append("image_ids", id));
  try {
    const response = await fetch(gallery.dataset.reorderUrl, {
      method: "POST",
      body,
      credentials: "same-origin",
      headers: { "HX-Request": "true", "Accept": "text/html" },
    });
    if (!response.ok) throw new Error("Could not save the new order");
    const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
    const updatedGallery = documentFragment.querySelector("#gallery");
    if (!updatedGallery) throw new Error("The gallery response was incomplete");
    gallery.replaceWith(updatedGallery);
  } catch (error) {
    window.location.reload();
  }
});

document.addEventListener("dragend", (event) => {
  event.target.closest?.(".image-card")?.classList.remove("is-dragging");
  document.querySelectorAll(".is-drop-target").forEach((card) => card.classList.remove("is-drop-target"));
  draggedImageId = null;
});
