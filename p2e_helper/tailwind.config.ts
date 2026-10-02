import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "#FBFBFA",
        ink: "#2F3437",
        muted: "#6B6F76",
        line: "#E9E9E7",
        warm: "#F7F6F3",
        sage: "#EEF3EF",
        clay: "#F4EDEA",
        gold: "#F7F1DF",
        mist: "#ECF2F5"
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        title: ["Georgia", "ui-serif", "serif"]
      }
    }
  },
  plugins: [require("@tailwindcss/typography")]
};

export default config;
