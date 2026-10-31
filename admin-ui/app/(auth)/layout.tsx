import "@/app/globals.css";
import { Plus_Jakarta_Sans } from "next/font/google";

const jakarta = Plus_Jakarta_Sans({ subsets: ["latin"], weight: ["500", "600", "800"], variable: "--font-jakarta" });

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <div className={jakarta.variable} style={{ display: "contents" }}>{children}</div>;
}
