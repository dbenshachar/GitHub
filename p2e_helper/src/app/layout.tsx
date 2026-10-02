import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PF2e Helper",
  description: "Local Pathfinder 2e character creator and sheet"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="font-sans">{children}</body>
    </html>
  );
}
