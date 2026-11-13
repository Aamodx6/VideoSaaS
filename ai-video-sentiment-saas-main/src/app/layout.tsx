import "~/styles/globals.css";

import { GeistSans } from "geist/font/sans";
import { type Metadata } from "next";
import { brand } from "~/lib/brand";

export const metadata: Metadata = {
  title: brand.name,
  applicationName: brand.name,
  description: brand.description,
  icons: [{ rel: "icon", url: "/aspect-sentiment.svg", type: "image/svg+xml" }],
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${GeistSans.variable}`}>
      <body>{children}</body>
    </html>
  );
}
