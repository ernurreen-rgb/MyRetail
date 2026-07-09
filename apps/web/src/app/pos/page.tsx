import { redirect } from "next/navigation";

import { POSManager } from "@/app/pos/pos-manager";
import { canUsePOS } from "@/lib/auth";
import { getAuthSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function POSPage() {
  const session = await getAuthSession();

  if (!session) {
    redirect("/login");
  }

  return (
    <POSManager
      canUsePOS={canUsePOS(session.user.roles)}
      userRoles={session.user.roles}
      userEmail={session.user.email}
    />
  );
}
