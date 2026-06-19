// Proxy /api/* to the FastAPI backend so the browser sees a single origin
// (keeps the HttpOnly SameSite=strict session cookie working in dev).
// Use 127.0.0.1 (not "localhost") so the proxy hits uvicorn's IPv4 bind even on
// Windows, where "localhost" can resolve to IPv6 ::1 first.
const backend = process.env.BACKEND_ORIGIN || "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/:path*` }];
  },
};

export default nextConfig;
