import SwiftUI

@main
struct TeVartaApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                ContentView()
            }
        }
    }
}

private struct ContentView: View {
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "newspaper")
                .font(.system(size: 44))
                .foregroundStyle(Color.accentColor)
            Text("Te Varta")
                .font(.largeTitle.bold())
            Text("Fixture-first app foundation")
                .foregroundStyle(.secondary)
        }
        .padding()
        .navigationTitle("Your world")
    }
}
