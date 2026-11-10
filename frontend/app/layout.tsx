import type { Metadata } from "next";
import { SessionProvider } from "@/components/SessionProvider";
import AppShell from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "FinBrain · Your private finance workspace",
  description:
    "Local-first financial auditing and a virtual paper trading workspace.",
  robots: { index: false, follow: false },
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <SessionProvider>
          <AppShell>{children}</AppShell>
        </SessionProvider>
      </body>
    </html>
  );
}
