using AzDdns.Services;
using Microsoft.AspNetCore.Authentication.OpenIdConnect;
using Microsoft.Identity.Web;
using Microsoft.Identity.Web.UI;

// ─── Load .env file ──────────────────────────────────────────────────────────
LoadDotEnv();

var builder = WebApplication.CreateBuilder(args);

// ─── Configuration from environment ──────────────────────────────────────────
var guiClientId = builder.Configuration["AZURE_APP_CLIENT_ID"]
                  ?? Environment.GetEnvironmentVariable("AZURE_APP_CLIENT_ID");

var guiAuthEnabled = !string.IsNullOrWhiteSpace(guiClientId);

// ─── Authentication / OIDC (optional) ────────────────────────────────────────
if (guiAuthEnabled)
{
    var tenantId = builder.Configuration["AZURE_TENANT_ID"]
                   ?? Environment.GetEnvironmentVariable("AZURE_TENANT_ID") ?? string.Empty;
    var guiClientSecret = builder.Configuration["AZURE_APP_CLIENT_SECRET"]
                          ?? Environment.GetEnvironmentVariable("AZURE_APP_CLIENT_SECRET");

    builder.Services
        .AddAuthentication(OpenIdConnectDefaults.AuthenticationScheme)
        .AddMicrosoftIdentityWebApp(options =>
        {
            options.Instance = "https://login.microsoftonline.com/";
            options.TenantId = tenantId;
            options.ClientId = guiClientId;
            options.ClientSecret = guiClientSecret;
            options.CallbackPath = "/signin-oidc";
        });

    builder.Services.AddAuthorization();
}

// ─── Application services ─────────────────────────────────────────────────────
builder.Services.AddSingleton<DnsCache>();
builder.Services.AddSingleton<AzureDnsService>();
builder.Services.AddSingleton<DnsConfigStore>();
builder.Services.AddSingleton<DnsUpdateService>();
builder.Services.AddHostedService<DnsBackgroundService>();

// ─── MVC / Razor Pages ────────────────────────────────────────────────────────
var mvcBuilder = builder.Services.AddControllersWithViews();
if (guiAuthEnabled)
    mvcBuilder.AddMicrosoftIdentityUI();

builder.Services.AddRazorPages();

// ─── Anti-forgery ─────────────────────────────────────────────────────────────
builder.Services.AddAntiforgery();

// ─── Build app ────────────────────────────────────────────────────────────────
var app = builder.Build();

if (!app.Environment.IsDevelopment())
{
    app.UseExceptionHandler("/error");
    app.UseHsts();
}

app.UseStaticFiles();
app.UseRouting();

if (guiAuthEnabled)
{
    app.UseAuthentication();
    app.UseAuthorization();
}

app.MapRazorPages();
app.MapControllers();

app.Run();

// ─── Helper: load .env file ───────────────────────────────────────────────────
static void LoadDotEnv()
{
    // Search from the app directory upward for a .env file
    var searchPaths = new[]
    {
        Path.Combine(AppContext.BaseDirectory, ".env"),
        Path.Combine(Directory.GetCurrentDirectory(), ".env"),
        Path.Combine(Directory.GetCurrentDirectory(), "..", ".env"),
        Path.Combine(AppContext.BaseDirectory, "..", ".env"),
    };

    string? envFile = searchPaths.FirstOrDefault(File.Exists);
    if (envFile is null)
        return;

    foreach (var rawLine in File.ReadAllLines(envFile))
    {
        var line = rawLine.Trim();
        if (string.IsNullOrEmpty(line) || line.StartsWith('#'))
            continue;

        var eqIdx = line.IndexOf('=');
        if (eqIdx <= 0)
            continue;

        var key = line[..eqIdx].Trim();
        var val = line[(eqIdx + 1)..].Trim().Trim('"').Trim('\'');

        // Only set if not already set by environment (container env takes priority)
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable(key)))
            Environment.SetEnvironmentVariable(key, val);
    }
}
