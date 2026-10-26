import "./globals.css";
import "katex/dist/katex.min.css";

import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages, getTranslations } from "next-intl/server";

import { PRODUCT_NAME } from "@/lib/branding";
import { Providers } from "@/components/providers";
import { localeDocumentTag } from "@/i18n/config";
import { fontVars } from "./fonts";

const BROWSER_EXTENSION_HYDRATION_GUARD_SCRIPT = `(function(){
  if (typeof window === "undefined" || typeof document === "undefined") return;
  var prefixes = ["bis_", "__processed_", "data-bis-"];
  function isExtAttr(name) {
    if (!name || typeof name !== "string") return false;
    var n = name.toLowerCase();
    for (var i = 0; i < prefixes.length; i++) {
      if (n.indexOf(prefixes[i]) === 0) return true;
    }
    return false;
  }
  function cleanEl(el) {
    if (!el || el.nodeType !== 1 || typeof el.getAttributeNames !== "function") return;
    var names = el.getAttributeNames();
    for (var i = 0; i < names.length; i++) {
      if (isExtAttr(names[i])) {
        el.removeAttribute(names[i]);
      }
    }
  }
  function cleanTree(root) {
    if (!root) return;
    cleanEl(root);
    if (typeof root.querySelectorAll === "function") {
      var all = root.querySelectorAll("[bis_skin_checked],[bis_register],[bis_id],[bis_size],[data-bis-config]");
      for (var i = 0; i < all.length; i++) {
        cleanEl(all[i]);
      }
    }
  }
  function isExtError(src, stack, msg) {
    var s = String(src || "") + "\\n" + String(stack || "");
    var m = String(msg || "");
    if (
      s.indexOf("chrome-extension://") !== -1 ||
      s.indexOf("moz-extension://") !== -1 ||
      s.indexOf("safari-web-extension://") !== -1
    ) {
      return true;
    }
    if (m.indexOf("reading 'M_ID'") !== -1) return true;
    return false;
  }
  window.addEventListener("error", function(e) {
    var src = e && e.filename;
    var stack = e && e.error && e.error.stack;
    var msg = e && e.message;
    if (isExtError(src, stack, msg)) {
      e.stopImmediatePropagation();
      e.preventDefault();
      return false;
    }
  }, true);
  window.addEventListener("unhandledrejection", function(e) {
    var r = e && e.reason;
    var stack = r && r.stack;
    var msg = (r && r.message) || String(r || "");
    if (isExtError("", stack, msg)) {
      e.stopImmediatePropagation();
      e.preventDefault();
      return false;
    }
  }, true);
  try {
    var origSetAttribute = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function(name, value) {
      if (isExtAttr(name)) return;
      return origSetAttribute.call(this, name, value);
    };
  } catch (_) {}
  cleanTree(document.documentElement);
  if (typeof MutationObserver !== "undefined" && document.documentElement) {
    var obs = new MutationObserver(function(mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var m = mutations[i];
        if (m.type === "attributes" && isExtAttr(m.attributeName)) {
          cleanEl(m.target);
        } else if (m.type === "childList" && m.addedNodes) {
          for (var j = 0; j < m.addedNodes.length; j++) {
            cleanTree(m.addedNodes[j]);
          }
        }
      }
    });
    obs.observe(document.documentElement, {
      attributes: true,
      childList: true,
      subtree: true
    });
  }
})();`;

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("Metadata");
  return { title: PRODUCT_NAME, description: t("description") };
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const locale = await getLocale();
  const messages = await getMessages();
  return (
    <html lang={localeDocumentTag(locale)} suppressHydrationWarning className={fontVars}>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: BROWSER_EXTENSION_HYDRATION_GUARD_SCRIPT,
          }}
        />
      </head>
      <body suppressHydrationWarning className="min-h-screen bg-background text-foreground antialiased">
        <NextIntlClientProvider locale={locale} messages={messages}>
          <Providers>{children}</Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
