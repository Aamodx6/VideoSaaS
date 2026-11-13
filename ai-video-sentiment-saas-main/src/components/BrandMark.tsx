import { brand } from "~/lib/brand";

export function BrandMark() {
  return (
    <div className="flex items-center gap-2">
      <span
        aria-hidden="true"
        className="flex h-8 w-8 items-center justify-center rounded-md bg-gray-800 text-sm font-semibold text-white"
      >
        {brand.initials}
      </span>
      <span className="text-lg font-medium">{brand.name}</span>
    </div>
  );
}
