const listingTypeField = document.getElementById("listing_type");
const listingStockField = document.getElementById("stock");
const listingStockContainer = document.getElementById("listing-stock-field");

function updateListingStockVisibility() {
  const isProduct = listingTypeField.value === "product";
  listingStockContainer.hidden = !isProduct;
  listingStockField.disabled = !isProduct;
  listingStockField.required = isProduct;
}

listingTypeField.addEventListener("change", updateListingStockVisibility);
updateListingStockVisibility();
