document.addEventListener("error", (event) => {
  const image = event.target;
  if (!(image instanceof HTMLImageElement)) return;
  image.hidden = true;
  const fallback = image.parentElement?.querySelector(".image-unavailable");
  if (fallback) fallback.hidden = false;
}, true);
