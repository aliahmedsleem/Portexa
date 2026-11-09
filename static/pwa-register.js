// PORTEXA PWA Registration
if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register(
            "/service-worker.js",
            { scope: "/" }
        ).then((registration) => {
            console.log(
                "PORTEXA PWA registered:",
                registration.scope
            );
        }).catch((error) => {
            console.error(
                "PORTEXA PWA registration failed:",
                error
            );
        });
    });
}
