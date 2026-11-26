defmodule App.MixProject do
  def project, do: [app: :ex_app]
  defp deps do
    [{:jason, "~> 1.4"}, {:{ORG}_auth, "~> 1.0", organization: "{ORG}"}]
  end
end
