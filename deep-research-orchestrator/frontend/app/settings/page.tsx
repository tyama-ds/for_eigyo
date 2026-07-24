import { t } from "@/lib/i18n";
import { ProfilesSection } from "@/components/settings/ProfilesSection";
import { RolesSection } from "@/components/settings/RolesSection";
import { ProxySection } from "@/components/settings/ProxySection";
import { SearchSection } from "@/components/settings/SearchSection";
import { AllowlistSection } from "@/components/settings/AllowlistSection";

export default function SettingsPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold text-slate-900">{t("settings.title")}</h1>
      <ProfilesSection />
      <RolesSection />
      <ProxySection />
      <SearchSection />
      <AllowlistSection />
    </div>
  );
}
