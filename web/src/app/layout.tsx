import "./globals.css";
import Link from "next/link";

export const metadata = {
  title: "Clinical Research Platform",
  description: "Unified protocol, compliance, dispatch, and AI assistant",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav className="nav">
          <strong>CRP</strong>
          <Link href="/">Dashboard</Link>
          <Link href="/subjects">Subjects</Link>
          <Link href="/trips">Trips</Link>
          <Link href="/protocol">Protocol</Link>
          <Link href="/chat">Chat</Link>
          <span className="spacer" />
          <Link href="/login">Sign in</Link>
        </nav>
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
