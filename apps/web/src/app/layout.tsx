import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SiteSense AI — Retail Location Intelligence",
  description:
    "A research workspace for weather-aware check-in activity modeling and retail location intelligence.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
